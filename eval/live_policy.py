"""Live, isolated V2 acceptance with complete production tool schemas.

No product patches during a run. Gold labels/checks are never sent to the model.
Only temporary files are mutated; source papers are copied and hash checked.

Suites: ``regression`` (8 debugged cases, default), ``holdout`` (eval/holdout_cases.py),
``all``. ``--live`` calls the paid API; ``--dry-run`` uses the scripted fake model in
eval/dryrun.py. Each turn is judged by eval/judge.py on files / status / answer /
constraints (required) and efficiency (observation only). report.json keeps the
legacy ``passed_local``; ``overall_accepted`` and judgement.json hold the new verdict.
Exit code: 0 overall accepted, 1 not accepted or suite incomplete, 2 infrastructure.
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

REGRESSION_TAGS = ['regression', 'debugged']
# Per-turn expectations for eval/judge.py. Accepted status sets are declared per
# case: needs_review (local checks passed, semantic review pending) is separate
# from partial/failed. Expectations never enter model context.
CASES = [
    dict(id='root_only', fixture='files', tags=REGRESSION_TAGS, turns=[
        '统计 Inbox 根目录中的 txt 文件数量，然后仅将根目录这些 txt 移入现有 Archive。'
        '子目录及其所有内容、PDF 和文件名都保持原样，不创建目录、不回收文件。'],
        # The count is not a local machine fact; the judge checks it in the answer,
        # so a review-only state is acceptable. partial is not.
        expect=dict(turns=[dict(status=['done', 'needs_review'],
                                answer=dict(include_any=[['2 个', '2个', '两个', '共 2']]),
                                constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                                efficiency_warn=dict(requests=8))])),
    dict(id='dedup_cleanup', fixture='duplicates', tags=REGRESSION_TAGS, turns=[
        '清理 Inbox 中字节完全相同的副本，每组保留文件名最短的一份；再回收其中的空文件夹。'
        '不同内容的文件和非空文件夹保持原样，不比较近似版本。'],
        expect=dict(turns=[dict(status=['done'], constraints=dict(effects=['trash'], write_scope=['Inbox']),
                                efficiency_warn=dict(requests=10))])),
    dict(id='real_classification', fixture='papers', tags=REGRESSION_TAGS, turns=[
        '请把文献中的全部 PDF 按论文核心方法分为四类，类别名固定为“强化学习”“Transformer”“综述”“其他”。'
        '综述优先归综述；不使用前两种方法且非综述的论文归其他。以正文为依据，不按文件名猜，证据不足标待核实。'
        '先只给分类方案和每类数量，不移动、改名或清理。将来执行时目标设为文献/分类，不编号。',
        '现在执行刚才保存的分类计划，按它的四类目录移动全部 PDF 到文献/分类，不编号、不改文件名，不清理旧目录。'],
        expect=dict(turns=[
            dict(status=['needs_review'], answer=dict(include_any=[['未移动', '尚未移动', '不移动']]),
                 constraints=dict(effects=[], forbid_tools=['write']), efficiency_warn=dict(requests=10)),
            dict(status=['done', 'needs_review'], constraints=dict(effects=['create', 'move'], write_scope=['文献']),
                 efficiency_warn=dict(requests=10))])),
    dict(id='application_dimension', fixture='applications', tags=REGRESSION_TAGS, turns=[
        '按应用领域将资料中的 PDF 分成“农业”“医疗”“能源”三类，不要按 CNN 等方法分类。'
        '直接整理到资料/按领域下面，不编号，不改文件名，也不要新增按方法目录；内容不能改变。'],
        expect=dict(turns=[dict(status=['done', 'needs_review'],
                                constraints=dict(effects=['create', 'move'], write_scope=['资料']),
                                efficiency_warn=dict(requests=12))])),
    dict(id='cross_turn_correction', fixture='files', tags=REGRESSION_TAGS, turns=[
        '仅将 Inbox 根目录的 a.txt、b.txt 移到现有 Archive，保留原名，其他内容保持原样，不创建目录也不回收。',
        '更正刚才的要求：b.txt 应留在 Inbox，请只把 b.txt 移回；a.txt 继续放在 Archive。'
        '更新原任务的最终要求，不要恢复其他文件，不要新建任务规避旧约束。'],
        expect=dict(turns=[
            dict(status=['done'], constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                 efficiency_warn=dict(requests=8)),
            dict(status=['done'], constraints=dict(effects=['move'], write_scope=['Inbox', 'Archive']),
                 efficiency_warn=dict(requests=8))])),
    dict(id='explanation_not_execution', fixture='duplicates', tags=REGRESSION_TAGS, turns=[
        '解释“删除重复文件”是什么意思。我现在只讨论方案，没有让你扫描或改动 Inbox。'],
        expect=dict(turns=[dict(status=['answered'],
                                answer=dict(exclude=['已移动', '已移入回收区', '已重命名', '已创建']),
                                constraints=dict(effects=[], forbid_tools=['scan', 'write']),
                                efficiency_warn=dict(requests=2, answer_chars=600))])),
    dict(id='no_answer', fixture='applications', tags=REGRESSION_TAGS, turns=[
        '在资料里找以 gravitational wave interferometry 为研究方法的论文。只报告有正文支持的结果。'
        '如果没有足够证据就直接说明，不能把低分或未命中说成穷尽排除，不改动文件。'],
        expect=dict(turns=[dict(status=['needs_review', 'answered', 'done'],
                                answer=dict(forbid_finding_relations=['method', 'survey', '方法相关', '综述入口']),
                                constraints=dict(effects=[], forbid_tools=['write']),
                                efficiency_warn=dict(requests=8, answer_chars=600))])),
    dict(id='target_conflict', fixture='conflict', tags=REGRESSION_TAGS, turns=[
        '把 Inbox/a.txt 移到现有 Archive，保留原名。目标若有同名文件，不覆盖、不改名、不回收任何文件，直接告诉我无法移动的原因。'],
        expect=dict(turns=[dict(status=['answered', 'partial', 'waiting_input'],
                                answer=dict(include_any=[['已存在', '同名']], exclude=['已移动']),
                                constraints=dict(effects=[], forbid_tools=['write']),
                                efficiency_warn=dict(requests=4))])),
]

from eval.holdout_cases import HOLDOUT_CASES, holdout_checks, holdout_fixture  # noqa: E402
from eval.blind_cases import BLIND_CASES, BLIND2_CASES, blind_checks, blind_fixture, blind2_checks, blind2_fixture  # noqa: E402
from eval.hidden_cases import HIDDEN_CASES, hidden_checks, hidden_fixture  # noqa: E402
from eval.p0_cases import P0_CASES, p0_checks, p0_fixture  # noqa: E402
from eval.p1_cases import P1_CASES, p1_checks, p1_fixture  # noqa: E402
from eval.product_cases import PRODUCT_CASES, product_checks, product_fixture  # noqa: E402
from eval.blind3_cases import BLIND3_CASES, blind3_checks, blind3_fixture  # noqa: E402
from eval.ask_cases import ASK_CASES, ask_checks, ask_fixture  # noqa: E402
from eval.newdir_cases import NEWDIR_CASES, newdir_checks, newdir_fixture  # noqa: E402

SUITES = {'regression': CASES, 'holdout': HOLDOUT_CASES, 'all': CASES + HOLDOUT_CASES, 'blind': BLIND_CASES, 'blind2': BLIND2_CASES, 'hidden': HIDDEN_CASES, 'p0': P0_CASES, 'p1': P1_CASES, 'product': PRODUCT_CASES, 'blind3': BLIND3_CASES, 'ask': ASK_CASES, 'newdir': NEWDIR_CASES}
CASE_SPECS = {case['id']: case for case in CASES + HOLDOUT_CASES}


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
    elif kind.startswith('blind_'):
        blind_fixture(root, kind)
    elif kind.startswith('blind2_'):
        blind2_fixture(root, kind)
    elif kind.startswith('hidden_'):
        hidden_fixture(root, kind)
    elif kind.startswith('p0_'):
        p0_fixture(root, kind)
    elif kind.startswith('p1_'):
        p1_fixture(root, kind)
    elif kind.startswith('prod_'):
        product_fixture(root, kind)
    elif kind.startswith('blind3_'):
        blind3_fixture(root, kind)
    elif kind.startswith('ask_'):
        ask_fixture(root, kind)
    elif kind.startswith('nd_'):
        newdir_fixture(root, kind)
    else:
        holdout_fixture(root, kind)


def labels(store):
    result = []
    for pid, plan in store.data['classification_plans'].items():
        rows = [dict(name=store.data['files'][a['file_id']]['name'], category=plan['categories'][a['category']-1],
                     uncertain=a['uncertain']) for a in plan['assignments']]
        result.append(dict(plan_id=pid, dimension=plan.get('dimension'), base_dir=plan['base_dir'],
                           numbered=plan.get('numbered'), status=plan['status'], assignments=rows))
    return result


NO_MATCH = '（评测：上一轮的选项中没有唯一符合的项）'


def _declares(option, want):
    """The option declares an action of this effect (on this path, when one is given)."""
    norm = lambda p: (p or '').strip().strip('/').removeprefix('./')
    return any(a['effect'] == want['effect'] and ('path' not in want or norm(a['path']) == norm(want['path']))
               for a in option.get('actions') or [])


def choose(turn, report):
    """A turn written as a choice stands for the user pressing the one matching option;
    option order is the model's, so a fixed letter cannot be frozen.

    {"choose_actions": {"has": [...], "lacks": [...]}} matches by the actions an option
    declares (round 22: labels are worded differently on every run, actions are not);
    {"choose": [words], "avoid": [...]} matches by label. No unique match sends a fixed
    text and is recorded."""
    if not isinstance(turn, dict):
        return turn, None
    options = (report or {}).get('choices') or []
    if 'choose_actions' in turn:
        rule = turn['choose_actions']
        hits = [c for c in options if all(_declares(c, w) for w in rule.get('has', []))
                and not any(_declares(c, w) for w in rule.get('lacks', []))]
        turn = dict(turn, choose=rule)
    else:
        hits = [c for c in options if any(w in c['label'] for w in turn['choose'])
                and not any(w in c['label'] for w in turn.get('avoid', []))]
    if len(hits) == 1:
        return hits[0]['key'], dict(words=turn['choose'], key=hits[0]['key'], label=hits[0]['label'])
    return NO_MATCH, dict(words=turn['choose'], key=None, matched=[c['label'] for c in hits],
                          offered=[c['label'] for c in options])


def checks(case_id, turn, root, before, store, runtime, first_task):
    if case_id.startswith('holdout_'):
        return holdout_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('blind_'):
        return blind_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('blind2_'):
        return blind2_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('hidden_'):
        return hidden_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('p0_'):
        return p0_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('p1_'):
        return p1_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('prod_'):
        return product_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('blind3_'):
        return blind3_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('ask_'):
        return ask_checks(case_id, turn, root, before, runtime, first_task)
    if case_id.startswith('nd_'):
        return newdir_checks(case_id, turn, root, before, runtime, first_task)
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


def _hashes(needs_papers=True):
    source = {path: digest(ROOT/'data/real/papers'/path) for path, _ in PAPERS} if needs_papers else {}
    code = {str(path.relative_to(ROOT)): digest(path) for folder in ('agent', 'tools', 'safety')
            for path in (ROOT/folder).glob('*.py')}
    return source, code


def run_suite(cases, out, cfg, total_cap, llm_for, mode):
    """Run cases in temporary libraries; ``llm_for(case_id, turn_index, cfg)`` builds the LLM.

    Writes manifest/report/judgement under ``out`` (must not exist) and returns
    (report, judgement, exit_code). The legacy ``passed_local`` fields are kept
    unchanged for comparison with frozen historical reports.
    """
    from eval.judge import exit_code, judge_suite, load_trace
    out.mkdir(parents=True, exist_ok=False)
    needs_papers = any(c['fixture'] == 'papers' for c in cases)
    source_hash, code_hash = _hashes(needs_papers)
    manifest = dict(mode=mode, cases=cases, real_paper_gold=PAPERS if needs_papers else [],
                    source_sha256=source_hash, code_sha256=code_hash,
                    config={k: v for k, v in cfg.items() if k != 'sandbox'}, total_soft_cap=total_cap)
    (out/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    report = dict(mode=mode, model=cfg['model'], cases=[], cost=0.0, actual_requests=0, evaluated=0,
                  limitations=['One sample per case, no paired baseline or reliability-rate claim.',
                               'Six real papers and three synthetic application documents; gold labels never enter model context.',
                               'Automatic approval in isolated temporary files does not validate human UI interaction.',
                               'Costs use project-configured rates, not billing reconciliation.',
                               'overall_accepted is a mechanical multi-dimension judgement; semantic correctness of needs_review items still needs human review.'])
    specs = {case['id']: case for case in cases}

    def judged():
        return judge_suite(specs, report, lambda cid: [load_trace(out/cid/f'turn_{i}.trace.jsonl')
                                                       for i in range(1, len(specs[cid]['turns']) + 1)])

    def save(final=False):
        report['evaluated'] = sum(c.get('status') != 'blocked' for c in report['cases'])
        report['passed_local'] = sum(c.get('passed_local', False) for c in report['cases'])
        if final:
            judgement = judged()
            report['overall_accepted'] = judgement['overall_accepted']
            report['judgement_summary'] = {k: v for k, v in judgement.items() if k != 'cases'}
            for row, case in zip(report['cases'], judgement['cases']):
                row['overall_accepted'] = case['overall_accepted']
                row['failed_dimensions'] = case['failed_dimensions']
            (out/'judgement.json').write_text(json.dumps(judgement, ensure_ascii=False, indent=2))
        (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    for case in cases:
        if report['cost'] >= total_cap: report['budget_stopped'] = True; break
        case_dir = out/case['id']; case_dir.mkdir()
        row = dict(id=case['id'], tags=case.get('tags', []), turns=[], checks={}, semantic_review='pending')
        with tempfile.TemporaryDirectory(prefix='filebutler-live-v2-') as tmp:
            from safety.sandbox import Sandbox
            library = Path(tmp)/'library' if case.get('isolate_parent') else Path(tmp)
            library.mkdir(exist_ok=True)
            sb = Sandbox(library); fixture(sb.root, case['fixture']); before = snapshot(sb.root, True)
            store = ArtifactStore(sb); history = []; first_task = None; last_report = None
            for index, turn in enumerate(case['turns']):
                instruction, chosen = choose(turn, last_report)
                if report['cost'] >= total_cap: report['budget_stopped'] = True; break
                turn_cfg = {**cfg, 'cost_budget': min(cfg['cost_budget'], total_cap - report['cost'])}
                llm = llm_for(case['id'], index, turn_cfg)
                requests = []; approvals = []; events = []; data = {}; result = None
                turn_before = snapshot(sb.root, True)
                if history:
                    history.append(dict(role='user', content=instruction))
                    history, projection = prepare_history(history, turn_cfg, store)
                else:
                    history = [dict(role='system', content=SYSTEM_PROMPTS[cfg['prompt']]), dict(role='user', content=instruction)]
                    projection = {}
                def approve(request): approvals.append(request); return 'y'
                def observe(event):
                    events.append(event)
                    if event['type'] == 'llm_end': requests.append(event)
                try:
                    result = run_agent(instruction, llm, sb, turn_cfg, Console(file=io.StringIO()), artifacts=store, history=history,
                                       oplog=OpLog(sb, f"{case['id']}_{index+1}"), confirmer=Confirmer('always', ask_action=approve),
                                       trace_path=case_dir/f'turn_{index+1}.trace.jsonl', on_event=observe)
                    data = asdict(result); history = data.pop('messages'); last_report = data.get('report')
                    if chosen: data['chosen_by_eval'] = chosen
                    safe_history = [{k: v for k, v in m.items() if k != 'reasoning_content'} for m in history]
                    (case_dir/f'turn_{index+1}.history.json').write_text(json.dumps(safe_history, ensure_ascii=False, indent=2))
                    (case_dir/f'turn_{index+1}.events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2))
                    (case_dir/f'turn_{index+1}.approvals.json').write_text(json.dumps(approvals, ensure_ascii=False, indent=2))
                    if not first_task and sb.runtime.current: first_task = sb.runtime.current['id']
                    data.update(checks=checks(case['id'], index, sb.root, before, store, sb.runtime, first_task),
                                task=sb.runtime.current, plans=labels(store), projection=projection, actual_requests=len(requests),
                                snapshot_before=turn_before, snapshot_after=snapshot(sb.root, True))
                    row['turns'].append(data)
                    report['actual_requests'] += len(requests)
                    for name, passed in data['checks'].items(): row['checks'][f'turn_{index+1}:{name}'] = passed
                except Exception as exc:
                    row['error_type'] = type(exc).__name__
                    if type(exc).__name__ in {'APIConnectionError', 'APITimeoutError', 'AuthenticationError', 'ModelConfigurationError'}:
                        row['status'] = 'blocked'; report['infrastructure_blocked'] = row['error_type']
                    else: row['checks']['no_exception'] = False
                    break
                finally:
                    report['cost'] += llm.cost
                    save()
                print(json.dumps(dict(case=case['id'], turn=index+1, stop=result.stop_reason, cost=llm.cost,
                                      active_seconds=result.active_seconds, model_calls=len(requests), checks=data['checks']),
                                 ensure_ascii=False), flush=True)
            row['checks']['all_turns_completed'] = len(row['turns']) == len(case['turns'])
            row['checks']['source_contents_preserved'] = files_preserved(before, snapshot(sb.root, True))
            row['before'] = before; row['after'] = snapshot(sb.root, True)
            # Verify undo against the original fixture after all turns, not each
            # intermediate task state. Unchanged runs need no journal; a turn
            # that raised still has its journal and must be undone too.
            undo_reports = []
            for index in reversed(range(len(case['turns']))):
                run_id = f"{case['id']}_{index+1}"
                if (sb.root/'.oplog'/f'{run_id}.jsonl').exists():
                    undo_reports.extend(undo(sb, run_id, on_restore=store.relocate))
            row['undo'] = undo_reports
            row['checks']['undo_restores_fixture'] = snapshot(sb.root, True) == before
        row['passed_local'] = bool(row['checks']) and all(row['checks'].values())
        report['cases'].append(row); save()
        if report.get('infrastructure_blocked'): break
    if needs_papers:
        report['source_papers_unchanged'] = all(digest(ROOT/'data/real/papers'/path) == value for path, value in source_hash.items())
    report['product_code_unchanged'] = all(digest(ROOT/path) == value for path, value in code_hash.items())
    report['suite_complete'] = len(report['cases']) == len(cases) and not report.get('budget_stopped') and not report.get('infrastructure_blocked')
    # Legacy口径 kept for comparison with frozen reports: physical checks only.
    report['legacy_passed_local_all'] = report['suite_complete'] and report['passed_local'] == len(cases)
    save(final=True)
    judgement = json.loads((out/'judgement.json').read_text())
    return report, judgement, exit_code(judgement, bool(report.get('infrastructure_blocked')))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--live', action='store_true', help='call the configured paid model API')
    mode.add_argument('--dry-run', action='store_true', help='scripted fake model, no API; pipeline check only')
    ap.add_argument('--suite', choices=sorted(SUITES), default='regression')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--cases', nargs='+', help='run only these case IDs of the suite')
    ap.add_argument('--total-cap', type=float, default=1.2)
    ap.add_argument('--protocol', choices=['v2', 'v3'], default=None,
                    help='model-visible task protocol; default from config.yaml (v2 = pre-P1-4 baseline)')
    args = ap.parse_args(argv)
    cfg = load_config(ROOT/'config.yaml')
    cfg.update(llm_retries=0, task_session='live-policy-acceptance')
    if args.protocol:
        cfg.update(protocol=args.protocol, prompt={'v2': 'v8_tasks', 'v3': 'v9_intent'}[args.protocol])
    cases = SUITES[args.suite]
    if args.cases:
        unknown = set(args.cases) - {c['id'] for c in cases}
        if unknown: ap.error('不在此套件中：' + '、'.join(sorted(unknown)))
        cases = [c for c in cases if c['id'] in args.cases]
    if args.dry_run:
        from eval.dryrun import SCRIPTS, scripted_llm_factory
        cfg.update(model='scripted-dry-run')
        llm_for = scripted_llm_factory(SCRIPTS)
    else:
        llm_for = lambda case_id, index, turn_cfg: LLM(turn_cfg)
    report, judgement, code = run_suite(cases, args.out, cfg, args.total_cap, llm_for, 'dry_run' if args.dry_run else 'live')
    print(json.dumps(dict(overall_accepted=judgement['overall_accepted'], cases_accepted=judgement['cases_accepted'],
                          cases_total=judgement['cases_total'], legacy_passed_local=report['passed_local'],
                          false_incomplete=judgement['false_incomplete'],
                          recovered_blockers_shown=judgement['recovered_blockers_shown'], exit_code=code),
                     ensure_ascii=False), flush=True)
    return code


if __name__=='__main__': raise SystemExit(main())
