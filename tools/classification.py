"""Versioned paper cards, complete classification plans and local batch moves.

The model chooses categories. Enumeration, excerpts, names, confirmation,
execution, verification and final Markdown stay local. No embeddings required.
"""
import html
import json
import os
import re
import unicodedata
import uuid
from collections import Counter
from pathlib import Path

from pydantic import BaseModel, Field
from agent.artifacts import digest, label
from tools.registry import LocalWriteBatch, ToolError, tool

PAGE_SIZE = 16


def _store(sb):
    store = getattr(sb, 'artifacts', None)
    if not store:
        raise ToolError('分类计划需要启用 local_runtime')
    return store


def _pdfs(sb, scope):
    root = sb.path(scope)
    if not root.is_dir():
        raise ToolError('分类范围必须是现有文件夹')
    result = []
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(base)/d).is_symlink())
        for name in sorted(files):
            p = Path(base)/name
            if not name.startswith('.') and p.suffix.lower() == '.pdf' and not p.is_symlink():
                result.append(sb.rel(p))
    if len(result) > 500:
        raise ToolError('本次最多分类 500 篇 PDF，请缩小目录范围')
    return result


def latest_plan(store):
    plans = store.data['classification_plans']
    return next(reversed(plans), None)


def category_directory(index,name):
    # Category labels such as 自监督/半监督 are display text, not paths.
    # Convert separators locally instead of asking the model to resubmit 46 IDs.
    return f'{index:02d}_'+name.replace('/','／').replace('\\','＼')


def _plan(store, plan_id):
    plan_id = latest_plan(store) if plan_id == 'latest' else plan_id
    if plan_id not in store.data['classification_plans']:
        raise ToolError('没有可执行的结构化分类计划；请先按核心方法分类。旧文字清单不会自动变成移动计划。')
    return plan_id, store.data['classification_plans'][plan_id]


def _profile(sb, fid):
    from tools.fs_read import _pdf_pages
    store = _store(sb)
    path, row = store.resolve(fid, allow_trash=False)
    old = store.data['paper_profiles'].get(fid)
    if old and old['version'] == row['version']:
        if not old.get('evidence_id') or store.resolve_evidence(old['evidence_id']):
            return old, True
    try:
        pages = _pdf_pages(path, pages=2)
        candidates = []
        for page, text in enumerate(pages, 1):
            hit = re.search(r'abstract\b|摘\s*要', text, re.I)
            offset = max(0, hit.start()-140) if hit else 0
            excerpt = text[offset:offset+600]
            candidates.append((bool(hit), len(text.strip()), page, offset, excerpt))
        _, length, page, offset, excerpt = max(candidates)
        if length < 40:
            raise ValueError('前两页无足够可提取文字')
        # Refuse binding text extracted during an external modification.
        store.resolve(fid, allow_trash=False)
        eid = store.evidence(fid, 'fragment', page=page, offset=offset, excerpt=excerpt,
                             scope='分类卡片：PDF前两页中的单页片段')
        profile = dict(version=row['version'], evidence_id=eid, page=page, text=excerpt, error='')
    except Exception as exc:
        profile = dict(version=row['version'], evidence_id=None, page=None, text='', error=type(exc).__name__)
    store.data['paper_profiles'][fid] = profile
    return profile, False


class ProfilesArgs(BaseModel):
    path: str = Field('.', description='待按核心方法分类的 PDF 目录；递归枚举可见文件')
    catalog_id: str | None = Field(None, description='翻页沿用首次返回的 C 标识，不重新枚举')
    page: int = Field(1, ge=1)


@tool('paper_profiles',
      '全量论文按核心方法分类的入口。一次在本地枚举 PDF 并批量返回 16 篇带 F/E 标识的原文卡片，无需先列目录或逐篇 read_file。'
      '结果告知总页数，可在同一轮读取剩余页。必须查看全部页后 submit_classification；证据不足标 uncertain，不按标题猜定论。',
      ProfilesArgs, max_result_chars=16000)
def paper_profiles(a, sb):
    store = _store(sb)
    if a.catalog_id:
        cid = a.catalog_id
        if cid not in store.data['catalogs']:
            raise ToolError('分类目录快照不存在，请重新获取 paper_profiles')
        catalog = store.data['catalogs'][cid]
    else:
        paths = _pdfs(sb, a.path)
        members = [store.register(p) for p in paths]
        cid = 'C'+uuid.uuid4().hex[:10]
        catalog = dict(path=sb.rel(sb.path(a.path)), members=members,
                       versions={fid:store.data['files'][fid]['version'] for fid in members}, delivered=[])
        store.data['catalogs'][cid] = catalog
    pages = max(1, (len(catalog['members'])+PAGE_SIZE-1)//PAGE_SIZE)
    if a.page > pages:
        raise ToolError(f'共 {pages} 页')
    cards, cached = [], 0
    for fid in catalog['members'][(a.page-1)*PAGE_SIZE:a.page*PAGE_SIZE]:
        _, row = store.resolve(fid, allow_trash=False)
        if row['version'] != catalog['versions'][fid]:
            raise ToolError('目录快照中的文件已变更，请重新获取卡片')
        profile, hit = _profile(sb, fid); cached += int(hit)
        excerpt=profile['text']
        text = html.escape(excerpt, quote=False)
        while len(text)>700:
            excerpt=excerpt[:-20]
            text=html.escape(excerpt,quote=False)
        evidence = f"evidence:{profile['evidence_id']} p={profile['page']}" if profile['evidence_id'] else '无可用原文，必须 uncertain=true'
        name=json.dumps(row['name'][:90],ensure_ascii=False)[:160]
        cards.append(f"file:{fid} {evidence} 名称={name}\n<file_content>{text}</file_content>")
        if fid not in catalog['delivered']:
            catalog['delivered'].append(fid)
    store._save()
    runtime = getattr(sb, 'runtime', None)
    if runtime:
        runtime.classification_stats = dict(catalog_id=cid, total=len(catalog['members']), delivered=len(catalog['delivered']), cached_cards=cached)
    return (f"OK: catalog_id={cid}；范围={catalog['path']}；PDF={len(catalog['members'])}；第 {a.page}/{pages} 页；本页缓存复用={cached}\n"
            '只按以下原文判断核心方法。查看全部页后提交 file_id→category，不复写长路径；不足项 uncertain=true。\n'+'\n'.join(cards))


class Assignment(BaseModel):
    file_id: str
    category: int = Field(ge=1, description='categories 中的序号，从 1 开始')
    uncertain: bool = Field(False, description='片段不足以确认核心方法时必须为 true')
    evidence_ids: list[str] | None = Field(None,max_length=5,description='通常省略，自动引用已提供卡片；补读其他页时填写所采用的完整 E 标识')


class SubmitArgs(BaseModel):
    catalog_id: str
    categories: list[str] = Field(min_length=1, max_length=30, description='简短类别名，每项最多55字；本地生成编号目录，标签中的斜杠自动转全角，不写路径')
    assignments: list[Assignment] = Field(max_length=500)
    note: str = Field('', max_length=200, description='仅必要说明或用户固定回复标记，不复写完整分类清单')


def classification_markdown(store, pid, plan):
    lines = [f"已形成 {len(plan['categories'])} 类、{len(plan['assignments'])} 篇 PDF 的分类计划，尚未移动文件。计划：`{pid}`。", '',
             '| 类别 | 文件 | 依据 |', '| --- | --- | --- |']
    for item in plan['assignments']:
        eids=item['evidence_ids']
        ev=store.data['evidence'].get(eids[0],{}) if eids else {}
        evidence = ' '.join(f"[片段{i+1}](evidence:{eid})" for i,eid in enumerate(eids)) if eids else '无可用片段'
        state = '；**待核实**' if item['uncertain'] else '；据片段分类'
        lines.append(f"| {label(plan['categories'][item['category']-1])} | {store.file_link(item['file_id'],ev.get('page'))} | {evidence}{state} |")
    lines += ['', '依据为前两页卡片及显式补读的所示片段，不代表通读全文；待核实项没有被当作已确认。回复“执行这个分类计划”可按本计划建立目录并移动，操作前会按现有确认模式展示完整清单。']
    if plan['note']: lines += ['', label(plan['note'])]
    return '\n'.join(lines)


@tool('submit_classification',
      '保存完整的核心方法分类计划并由本地生成最终分类表。categories 为类别名列表，assignments 只填 F 标识、类别序号与 uncertain。'
      '必须覆盖目录快照每个 PDF 且恰好一次；片段不足时标 uncertain。提交成功后不要再生成长路径清单；不会移动文件。', SubmitArgs)
def submit_classification(a, sb):
    store = _store(sb)
    catalog = store.data['catalogs'].get(a.catalog_id)
    if not catalog:
        raise ToolError('目录快照不存在')
    if set(catalog['delivered']) != set(catalog['members']):
        raise ToolError('尚未查看全部分类卡片，请用同一 catalog_id 读取剩余页后提交')
    ids = [item.file_id.removeprefix('file:') for item in a.assignments]
    if len(ids) != len(set(ids)) or set(ids) != set(catalog['members']):
        missing = sorted(set(catalog['members'])-set(ids))
        raise ToolError(f'分类必须完整且无重复；缺少 F 标识：{missing[:15]}；不能混入其他快照文件')
    categories = [unicodedata.normalize('NFC', x).strip() for x in a.categories]
    if len({x.casefold() for x in categories}) != len(categories):
        raise ToolError('类别名不能重复')
    for name in categories:
        if (not name or len(name)>55 or name.startswith(('.','/','\\')) or re.search(r'[\x00-\x1f]',name)
                or any(p.startswith('.') or not p for p in re.split(r'[/\\]',name))):
            raise ToolError('类别名须为 1–55 字的可见标签，不能是绝对路径、隐藏项或路径穿越')
    assignments=[]
    for item,fid in zip(a.assignments,ids):
        _, row = store.resolve(fid, allow_trash=False)
        if row['version'] != catalog['versions'][fid] or item.category > len(categories):
            raise ToolError('文件版本或类别序号不正确')
        profile = store.data['paper_profiles'][fid]
        eids=[e.removeprefix('evidence:') for e in item.evidence_ids] if item.evidence_ids is not None else ([profile['evidence_id']] if profile.get('evidence_id') else [])
        for eid in eids:
            ev=store.resolve_evidence(eid)
            if ev['file_id']!=fid or ev['kind']!='fragment':raise ToolError('分类证据不属于当前文件的原文片段')
        if not eids and not item.uncertain:
            raise ToolError(f'file:{fid} 没有可用正文，必须标 uncertain=true')
        assignments.append(dict(file_id=fid, category=item.category, uncertain=item.uncertain,evidence_ids=eids))
    if set(_pdfs(sb,catalog['path'])) != {store.data['files'][fid]['path'] for fid in ids}:
        raise ToolError('目录文件清单已变化，请重新获取分类快照')
    pid='P'+uuid.uuid4().hex[:10]
    scope=Path(catalog['path'])
    base=(scope/'按方法').as_posix()
    plan=dict(path=catalog['path'], base_dir=base, versions=dict(catalog['versions']),
              categories=categories, assignments=assignments, note=a.note, status='ready')
    store.data['classification_plans'][pid]=plan;store._save()
    runtime=getattr(sb,'runtime',None)
    if runtime and runtime.task=='classification':
        runtime.final_answer=classification_markdown(store,pid,plan)
    return f'OK: 已保存分类计划 {pid}，共 {len(ids)} 篇；分类表已由本地生成，尚未移动文件。'


class ApplyArgs(BaseModel):
    plan_id: str = Field('latest', description='明确要执行的 P 标识；指代刚才方案时填 latest')


def _safe_target(sb, rel):
    parts=Path(rel).parts
    if Path(rel).is_absolute() or any(x.startswith('.') for x in parts if x!='.'):
        raise ToolError('分类目标不能是隐藏或越界路径')
    cursor=sb.root
    for part in parts:
        cursor=cursor/part
        if cursor.is_symlink():
            raise ToolError('分类目标不能经过符号链接')
    return sb.path(rel,write=True,exact=True)


def _move_plan(sb, pid, plan):
    store=_store(sb);moves=[];targets=set()
    _safe_target(sb,plan['base_dir'])
    for item in plan['assignments']:
        fid=item['file_id'];source,row=store.resolve(fid,allow_trash=False)
        if row['version'] != plan['versions'][fid] or digest(source) != plan['versions'][fid]:
            raise ToolError('分类后文件内容已变化，请重新分类')
        directory=f"{plan['base_dir']}/{category_directory(item['category'],plan['categories'][item['category']-1])}"
        target=_safe_target(sb,(Path(directory)/source.name).as_posix())
        # Resolve platform name collisions, including case/Unicode aliases.
        key=unicodedata.normalize('NFKC',str(target)).casefold()
        if key in targets:
            raise ToolError('同一分类目标存在同名文件，不会覆盖；请先处理重名')
        targets.add(key)
        if target.exists() and target != source:
            raise ToolError(f'目标位置已存在文件，不会覆盖：{sb.rel(target)}')
        parent=target.parent
        while parent != sb.root:
            if parent.exists() and not parent.is_dir():
                raise ToolError('分类目标的上级路径被文件占用')
            parent=parent.parent
        moves.append(dict(file_id=fid,src=sb.rel(source),dst=sb.rel(target),dst_dir=directory,
                          uncertain=item['uncertain'], category=item['category']))
    return moves


def _execute(sb, oplog, confirmer, pid, plan):
    from tools.fs_write import make_dir,MakeDirArgs
    if not oplog:
        raise ToolError('批量分类需要操作日志，才能撤销')
    store=_store(sb);runtime=getattr(sb,'runtime',None)
    moved=[];created=[]
    def finish(error=''):
        plan['last_execution']=dict(run_id=store.run_id,moved=list(moved),created=list(created),error=error,verified=not error)
        plan['status']='partial' if error else 'applied';store._save()
        counts=Counter(x['category'] for x in plan['assignments'])
        text=f"{'批量移动未全部完成' if error else '分类计划执行完成'}：已移动 {len(moved)} 篇，创建 {len(created)} 个文件夹。"
        if error:
            text+=f'\n{label(error)}\n已记录的操作可撤销；未报告为全部成功。'
        else:
            text+='\n本地逐项复核所有计划文件的位置与 SHA256 通过；未覆盖同名文件。\n\n| 分类目录 | 计划篇数 |\n| --- | ---: |'
            for category,count in sorted(counts.items()):
                text+=f"\n| {label(plan['base_dir'])}/{label(category_directory(category,plan['categories'][category-1]))} | {count} |"
            text+='\n\n| 文件 | 分类 |\n| --- | --- |'
            for item in plan['assignments']:
                text+=f"\n| {store.file_link(item['file_id'])} | {label(plan['categories'][item['category']-1])}"+('（待核实）' if item['uncertain'] else '')+' |'
            text+='\n\n仅执行本计划，后续新增文件不在本次计划内；本轮可撤销。'
        if plan['note']:text+='\n\n'+label(plan['note'])
        if runtime and runtime.task=='classification_apply':
            runtime.final_answer=text
            if error:runtime.stop_reason='operation_error'
        return ('ERROR: ' if error else 'OK: ')+f'已移动 {len(moved)} 篇，新增目录 {len(created)} 个；'+(error or '本地逐文件 SHA256/位置复核通过，结果已生成。')
    moves=_move_plan(sb,pid,plan)
    affected=[m for m in moves if m['src']!=m['dst']]
    if affected and confirmer and not confirmer.allow('move_many',{'plan_id':pid,'moves':affected},file_count=len(affected)):
        if runtime and runtime.task=='classification_apply':runtime.final_answer='你已拒绝执行分类计划，未创建目录或移动文件。'
        return 'ERROR: 用户拒绝了分类移动，未改动文件'
    # Confirmation may take minutes: revalidate the whole plan before mutation.
    moves=_move_plan(sb,pid,plan)
    if hasattr(sb,'progress'):sb.progress(f'本地执行分类计划：{len(affected)} 篇，逐项记录并复核')
    try:
        for directory in dict.fromkeys(m['dst_dir'] for m in moves):
            result=make_dir(MakeDirArgs(path=directory),sb)
            if isinstance(result,tuple):
                for op,src,dst in result[1]:
                    try:oplog.record(op,src,dst)
                    except Exception:
                        # Remove this and any as-yet unjournalled empty parents.
                        for _,_,pending in reversed(result[1]):
                            p=sb.path(pending,exact=True)
                            if pending not in created and p.is_dir() and not any(p.iterdir()):p.rmdir()
                        raise
                    created.append(dst)
        for item in moves:
            if item['src']==item['dst']:continue
            source,_=store.resolve(item['file_id'],allow_trash=False)
            target=_safe_target(sb,item['dst'])
            if sb.rel(source)!=item['src'] or digest(source)!=plan['versions'][item['file_id']] or target.exists():
                raise ToolError('执行期间文件或目标发生变化，已停止')
            # Exclusive hard-link creation avoids rename-overwrite races on the
            # local filesystem. Unlink only after the destination is reserved.
            os.link(source,target)
            try:source.unlink()
            except Exception:
                target.unlink();raise
            try:oplog.record('move',item['src'],item['dst'])
            except Exception:
                # Restore without overwriting an externally-created source.
                try:os.link(target,source)
                except FileExistsError:raise ToolError(f"日志失败且原路径被占用；文件保留于 {item['dst']}，需手动恢复")
                target.unlink();store.relocate(item['src'],item['src']);raise
            moved.append(item['file_id']);store.relocate(item['src'],item['dst'])
        for item in moves:
            target=_safe_target(sb,item['dst'])
            if not target.is_file() or digest(target)!=plan['versions'][item['file_id']]:
                raise ToolError('执行后文件位置/内容复核失败')
            if item['src']!=item['dst'] and (sb.path(item['src'],exact=True).exists() or sb.path(item['src'],exact=True).is_symlink()):
                raise ToolError('执行后原位置仍有文件，需核对外部改动')
        return finish()
    except Exception as exc:
        return finish(f'{type(exc).__name__}: {exc}')


@tool('apply_classification',
      '仅在用户明确授权执行已保存分类计划时使用。本地一次校验全部版本/目标、展示逐文件确认、建目录、移动、记日志、复核并生成最终表格。'
      '不需要逐个 make_dir/move，也不需要后续模型总结。', ApplyArgs,writes=True,batch=True)
def apply_classification(a,sb):
    store=_store(sb);runtime=getattr(sb,'runtime',None)
    if not runtime or not runtime.allow_classification_apply:
        raise ToolError('本轮未授权执行分类计划，只能查看分类结果')
    pid,plan=_plan(store,a.plan_id)
    count=re.search(r'(\d+)\s*类',runtime.instruction)
    if count and int(count.group(1))!=len(plan['categories']):
        raise ToolError('指令中的分类数与最新计划不符，请明确要执行的计划')
    return LocalWriteBatch(lambda sandbox,oplog,confirmer:_execute(sandbox,oplog,confirmer,pid,plan))
