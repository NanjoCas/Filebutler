"""Live, isolated V2 acceptance with complete production tool schemas.

No product patches during a run. Gold labels/checks are never sent to the model.
Only temporary files are mutated; source papers are copied and hash checked.
"""
import argparse
import hashlib
import io
import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

import pymupdf
from rich.console import Console

from agent.artifacts import ArtifactStore, digest
from agent.history import prepare_history
from agent.llm import LLM
from agent.loop import run_agent
from agent.prompts import SYSTEM_PROMPTS
from eval.checker import files_preserved, snapshot
from main import ROOT, load_config
from safety.confirm import Confirmer
from safety.oplog import OpLog, undo


PAPERS = [
    ('1912.12520v2.pdf', '强化学习'),
    ('2202.08159v1.pdf', '强化学习'),
    ('Big Self-Supervised Models are Strong Semi-Supervised Learners.pdf', '其他'),
    ('Dong.pdf', '其他'),
    ('review/Deep learning for fake_news_detection_A_comprehensive_survey.pdf', '综述'),
    ('Pre-training of Deep Bidirectional Transformers for Language Understanding.pdf', 'Transformer'),
]

CASES = [
    dict(id='root_only', fixture='files', turns=[
        '统计 Inbox 根目录中的 txt 文件数量，然后仅将根目录这些 txt 移入现有 Archive。'
        '子目录及其所有内容、PDF 和文件名都保持原样，不创建目录、不回收文件。']),
    dict(id='dedup_cleanup', fixture='duplicates', turns=[
        '清理 Inbox 中字节完全相同的副本，每组保留文件名最短的一份；再回收其中的空文件夹。'
        '不同内容的文件和非空文件夹保持原样，不比较近似版本。']),
    dict(id='real_classification', fixture='papers', turns=[
        '请把文献中的全部 PDF 按论文核心方法分为四类，类别名固定为“强化学习”“Transformer”“综述”“其他”。'
        '综述优先归综述；不使用前两种方法且非综述的论文归其他。以正文为依据，不按文件名猜，证据不足标待核实。'
        '先只给分类方案和每类数量，不移动、改名或清理。将来执行时目标设为文献/分类，不编号。',
        '现在执行刚才保存的分类计划，按它的四类目录移动全部 PDF 到文献/分类，不编号、不改文件名，不清理旧目录。']),
    dict(id='application_dimension', fixture='applications', turns=[
        '按应用领域将资料中的 PDF 分成“农业”“医疗”“能源”三类，不要按 CNN 等方法分类。'
        '直接整理到资料/按领域下面，不编号，不改文件名，也不要新增按方法目录；内容不能改变。']),
    dict(id='cross_turn_correction', fixture='files', turns=[
        '仅将 Inbox 根目录的 a.txt、b.txt 移到现有 Archive，保留原名，其他内容保持原样，不创建目录也不回收。',
        '更正刚才的要求：b.txt 应留在 Inbox，请只把 b.txt 移回；a.txt 继续放在 Archive。'
        '更新原任务的最终要求，不要恢复其他文件，不要新建任务规避旧约束。']),
    dict(id='explanation_not_execution', fixture='duplicates', turns=[
        '解释“删除重复文件”是什么意思。我现在只讨论方案，没有让你扫描或改动 Inbox。']),
    dict(id='no_answer', fixture='applications', turns=[
        '在资料里找以 gravitational wave interferometry 为研究方法的论文。只报告有正文支持的结果。'
        '如果没有足够证据就直接说明，不能把低分或未命中说成穷尽排除，不改动文件。']),
    dict(id='target_conflict', fixture='conflict', turns=[
        '把 Inbox/a.txt 移到现有 Archive，保留原名。目标若有同名文件，不覆盖、不改名、不回收任何文件，直接告诉我无法移动的原因。']),
]


def pdf(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(40, 40, 550, 800), text, fontsize=11)
        document.save(path)


def fixture(root, kind):
    if kind in {'files', 'conflict'}:
        for rel in ('Inbox/nested', 'Archive'): (root/rel).mkdir(parents=True)
        for rel, text in [('Inbox/a.txt','alpha'),('Inbox/b.txt','beta'),('Inbox/nested/keep.txt','protected nested record')]:
            (root/rel).write_text(text)
        pdf(root/'Inbox/keep.pdf', 'Document that must stay in its original location.')
        if kind == 'conflict': (root/'Archive/a.txt').write_text('different destination contents')
    elif kind == 'duplicates':
        (root/'Inbox/empty').mkdir(parents=True)
        (root/'Inbox/nonempty').mkdir()
        for rel, text in [('a.txt','same exact bytes'),('copy_of_a.txt','same exact bytes'),('unique.txt','different'),('nonempty/keep.txt','protected')]:
            (root/'Inbox'/rel).write_text(text)
    elif kind == 'papers':
        (root/'文献').mkdir()
        for i, (source, _) in enumerate(PAPERS, 1):
            shutil.copyfile(ROOT/'data/real/papers'/source, root/'文献'/f'doc_{i:02d}.pdf')
    elif kind == 'applications':
        texts = [
            ('agriculture', 'Abstract: We train a convolutional neural network (CNN) to identify crop leaf disease in wheat fields. The application is agricultural crop protection, not clinical diagnosis or electricity systems.'),
            ('clinical', 'Abstract: We train a convolutional neural network (CNN) to diagnose lung disease using clinical chest X-ray images. This work is medical diagnosis, not agriculture or electricity generation.'),
            ('energy', 'Abstract: We train a convolutional neural network (CNN) to forecast solar power production and electricity demand for power-grid operation. The application is energy systems, not agriculture or medicine.'),
        ]
        for i, (_, text) in enumerate(texts, 1): pdf(root/'资料'/f'CNN_{i:02d}.pdf', text)


def labels(store):
    result = []
    for pid, plan in store.data['classification_plans'].items():
        rows = [dict(name=store.data['files'][a['file_id']]['name'], category=plan['categories'][a['category']-1],
                     uncertain=a['uncertain']) for a in plan['assignments']]
        result.append(dict(plan_id=pid, dimension=plan.get('dimension'), base_dir=plan['base_dir'],
                           numbered=plan.get('numbered'), status=plan['status'], assignments=rows))
    return result


def checks(case_id, turn, root, before, store, runtime, first_task):
    after = snapshot(root, True)
    result = dict(contents_preserved=files_preserved(before, after))
    if case_id in {'root_only', 'cross_turn_correction'}:
        result.update(a_moved=(root/'Archive/a.txt').is_file() and not (root/'Inbox/a.txt').exists(),
            nested_unchanged=after.get('Inbox/nested/keep.txt')==before['Inbox/nested/keep.txt'],
            pdf_unchanged=(root/'Inbox/keep.pdf').is_file())
        corrected = case_id=='cross_turn_correction' and turn==1
        result['b_location'] = (root/('Inbox/b.txt' if corrected else 'Archive/b.txt')).is_file()
        result['b_old_location_absent'] = not (root/('Archive/b.txt' if corrected else 'Inbox/b.txt')).exists()
        result['a_contents_correct'] = after.get('Archive/a.txt') == before['Inbox/a.txt']
        result['b_contents_correct'] = after.get('Inbox/b.txt' if corrected else 'Archive/b.txt') == before['Inbox/b.txt']
        result['others_unchanged'] = all(after.get(path)==value for path,value in before.items()
                                        if path not in {'Inbox/a.txt','Inbox/b.txt'})
        if corrected:
            result['same_task_updated'] = bool(runtime.current and runtime.current['id']==first_task and runtime.current['revision']>1)
    elif case_id == 'dedup_cleanup':
        result.update(keeper_retained=(root/'Inbox/a.txt').is_file(), duplicate_trashed=not (root/'Inbox/copy_of_a.txt').exists(),
                      empty_removed=not (root/'Inbox/empty').exists(), unique_retained=after.get('Inbox/unique.txt')==before['Inbox/unique.txt'],
                      nonempty_retained=after.get('Inbox/nonempty/keep.txt')==before['Inbox/nonempty/keep.txt'])
    elif case_id == 'real_classification':
        plans=labels(store)
        assignments=plans[-1]['assignments'] if plans else []
        expected={f'doc_{i:02d}.pdf':category for i, (_,category) in enumerate(PAPERS,1)}
        result['complete_assignments']=len(assignments)==len(expected) and {a['name'] for a in assignments}==set(expected)
        result['labels_match_gold']=bool(assignments) and all(a['category']==expected.get(a['name']) for a in assignments)
        if turn==0: result['readonly']=after==before
        else:
            result['correct_final_locations']=all(after.get(f'文献/分类/{category}/{name}')==before[f'文献/{name}'] for name,category in expected.items())
            result['original_locations_absent']=all(not (root/'文献'/name).exists() for name in expected)
    elif case_id == 'application_dimension':
        result['correct_final_locations']=all(after.get(f'资料/按领域/{category}/CNN_{i:02d}.pdf')==before[f'资料/CNN_{i:02d}.pdf']
                                              for i,category in enumerate(('农业','医疗','能源'),1))
        result['no_method_directory']=not any(p.name=='按方法' for p in root.rglob('*'))
    elif case_id in {'explanation_not_execution', 'no_answer', 'target_conflict'}:
        result['readonly']=after==before
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--live',action='store_true',required=True)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--total-cap',type=float,default=1.2)
    args=ap.parse_args(); args.out.mkdir(parents=True,exist_ok=False)
    cfg=load_config(ROOT/'config.yaml')
    cfg.update(llm_retries=0, task_session='live-policy-acceptance')
    source_hash={path:digest(ROOT/'data/real/papers'/path) for path,_ in PAPERS}
    code_hash={str(path.relative_to(ROOT)):digest(path) for folder in ('agent','tools','safety') for path in (ROOT/folder).glob('*.py')}
    manifest=dict(cases=CASES, real_paper_gold=PAPERS, source_sha256=source_hash, code_sha256=code_hash,
                  config={k:v for k,v in cfg.items() if k!='sandbox'}, total_soft_cap=args.total_cap)
    (args.out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    report=dict(model=cfg['model'], cases=[], cost=0.0, actual_requests=0, evaluated=0,
                limitations=['One sample per case, no paired baseline or reliability-rate claim.',
                             'Six real papers and three synthetic application documents; gold labels never enter model context.',
                             'Automatic approval in isolated temporary files does not validate human UI interaction.',
                             'Costs use project-configured rates, not billing reconciliation.'])
    def save():
        report['evaluated']=sum(c.get('status')!='blocked' for c in report['cases'])
        report['passed_local']=sum(c.get('passed_local',False) for c in report['cases'])
        (args.out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    for case in CASES:
        if report['cost'] >= args.total_cap: report['budget_stopped']=True; break
        case_dir=args.out/case['id']; case_dir.mkdir()
        row=dict(id=case['id'], turns=[], checks={}, semantic_review='pending')
        with tempfile.TemporaryDirectory(prefix='filebutler-live-v2-') as tmp:
            from safety.sandbox import Sandbox
            sb=Sandbox(tmp); fixture(sb.root,case['fixture']); before=snapshot(sb.root,True)
            store=ArtifactStore(sb); history=[]; first_task=None
            for index,instruction in enumerate(case['turns']):
                if report['cost'] >= args.total_cap: report['budget_stopped']=True; break
                llm=LLM({**cfg,'cost_budget':min(cfg['cost_budget'],args.total_cap-report['cost'])})
                turn_cfg={**cfg,'cost_budget':min(cfg['cost_budget'],args.total_cap-report['cost'])}
                requests=[]; approvals=[]; events=[]
                if history:
                    history.append(dict(role='user',content=instruction))
                    history,projection=prepare_history(history,turn_cfg,store)
                else:
                    history=[dict(role='system',content=SYSTEM_PROMPTS[cfg['prompt']]),dict(role='user',content=instruction)]
                    projection={}
                def approve(request): approvals.append(request); return 'y'
                def observe(event):
                    events.append(event)
                    if event['type']=='llm_end': requests.append(event)
                try:
                    result=run_agent(instruction,llm,sb,turn_cfg,Console(file=io.StringIO()),artifacts=store,history=history,
                                     oplog=OpLog(sb,f"{case['id']}_{index+1}"),confirmer=Confirmer('always',ask_action=approve),
                                     trace_path=case_dir/f'turn_{index+1}.trace.jsonl',on_event=observe)
                    data=asdict(result); history=data.pop('messages')
                    safe_history=[{k:v for k,v in m.items() if k!='reasoning_content'} for m in history]
                    (case_dir/f'turn_{index+1}.history.json').write_text(json.dumps(safe_history,ensure_ascii=False,indent=2))
                    (case_dir/f'turn_{index+1}.events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
                    (case_dir/f'turn_{index+1}.approvals.json').write_text(json.dumps(approvals,ensure_ascii=False,indent=2))
                    if not first_task and sb.runtime.current: first_task=sb.runtime.current['id']
                    data.update(checks=checks(case['id'],index,sb.root,before,store,sb.runtime,first_task),
                                task=sb.runtime.current, plans=labels(store), projection=projection, actual_requests=len(requests))
                    row['turns'].append(data)
                    report['actual_requests']+=len(requests)
                    for name,passed in data['checks'].items(): row['checks'][f'turn_{index+1}:{name}']=passed
                except Exception as exc:
                    row['error_type']=type(exc).__name__
                    if type(exc).__name__ in {'APIConnectionError','APITimeoutError','AuthenticationError','ModelConfigurationError'}:
                        row['status']='blocked';report['infrastructure_blocked']=row['error_type']
                    else: row['checks']['no_exception']=False
                    break
                finally:
                    report['cost']+=llm.cost
                    save()
                print(json.dumps(dict(case=case['id'],turn=index+1,stop=result.stop_reason,cost=llm.cost,
                    active_seconds=result.active_seconds,model_calls=len(requests),checks=data['checks']),ensure_ascii=False),flush=True)
            row['checks']['all_turns_completed']=len(row['turns'])==len(case['turns'])
            row['checks']['source_contents_preserved']=files_preserved(before,snapshot(sb.root,True))
            row['before']=before;row['after']=snapshot(sb.root,True)
            # Verify undo against the original fixture after all turns, not each
            # intermediate task state. Unchanged runs need no journal.
            undo_reports=[]
            for index in reversed(range(len(row['turns']))):
                run_id=f"{case['id']}_{index+1}"
                if (sb.root/'.oplog'/f'{run_id}.jsonl').exists():
                    undo_reports.extend(undo(sb,run_id,on_restore=store.relocate))
            row['undo']=undo_reports
            row['checks']['undo_restores_fixture']=snapshot(sb.root,True)==before
        row['passed_local']=bool(row['checks']) and all(row['checks'].values())
        report['cases'].append(row);save()
        if report.get('infrastructure_blocked'): break
    report['source_papers_unchanged']=all(digest(ROOT/'data/real/papers'/path)==value for path,value in source_hash.items())
    report['product_code_unchanged']=all(digest(ROOT/path)==value for path,value in code_hash.items())
    report['suite_complete']=len(report['cases'])==len(CASES) and not report.get('budget_stopped') and not report.get('infrastructure_blocked')
    save()
    return 2 if report.get('infrastructure_blocked') else 0 if report['suite_complete'] and report['passed_local']==len(CASES) else 1


if __name__=='__main__': raise SystemExit(main())
