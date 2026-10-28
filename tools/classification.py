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
from pathlib import Path

from pydantic import BaseModel, Field
from agent.artifacts import digest, label
from agent.policy import EvidencePolicy, evidence_policy
from tools.registry import ToolError, tool
from tools.scopes import catalog_scope

PAGE_SIZE = 16


def _store(sb):
    store = getattr(sb, 'artifacts', None)
    if not store:
        raise ToolError('分类计划需要文件引用存储（artifacts）')
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


def category_directory(index,name,numbered=True):
    # Category labels such as 自监督/半监督 are display text, not paths.
    # Convert separators locally instead of asking the model to resubmit 46 IDs.
    return (f'{index:02d}_' if numbered else '')+name.replace('/','／').replace('\\','＼')


def plan_directory(plan, index):
    explicit = plan.get('category_paths', {}).get(str(index))
    return explicit or category_directory(index, plan['categories'][index-1], plan.get('numbered', True))


def _plan(store, plan_id):
    plan_id = latest_plan(store) if plan_id == 'latest' else plan_id
    plans = store.data['classification_plans']
    if plan_id not in plans:
        if not plans:
            raise ToolError('没有可执行的结构化分类计划；请先保存分类方案。旧文字清单不会自动变成移动计划。')
        # 2.0 P0-B: a mistyped ID gets the saved plans as candidates, never a silent correction.
        recent = list(plans.items())[-3:][::-1]
        raise ToolError(f'分类计划 {plan_id} 不存在。可用候选：' + '；'.join(
            f"{pid}（{'/'.join(p['categories'])}，{len(p['assignments'])} 篇）" for pid, p in recent)
            + '。请照抄其中一个 ID 重新提交，不要重新保存分类方案')
    return plan_id, store.data['classification_plans'][plan_id]


def _profile(sb, fid, policy=None):
    store = _store(sb)
    path, row = store.resolve(fid, allow_trash=False)
    policy = policy or evidence_policy(sb)
    policy_key = policy.model_dump()
    old = store.data['paper_profiles'].get(fid)
    if old and old['version'] == row['version'] and old.get('policy', EvidencePolicy().model_dump()) == policy_key:
        if not old.get('evidence_id') or store.resolve_evidence(old['evidence_id']):
            return old, True
    try:
        from agent.extract import units
        pages = units(sb, path, pages=policy.pdf_pages)
        candidates = []
        for page, text in enumerate(pages, 1):
            hit = re.search(r'abstract\b|摘\s*要', text, re.I)
            offset = max(0, hit.start()-140) if hit else 0
            excerpt = text[offset:offset+policy.excerpt_chars]
            candidates.append((bool(hit), len(text.strip()), page, offset, excerpt))
        _, length, page, offset, excerpt = max(candidates)
        if length < 40:
            raise ValueError('选定范围无足够可提取文字')
        # Refuse binding text extracted during an external modification.
        store.resolve(fid, allow_trash=False)
        eid = store.evidence(fid, 'fragment', page=page, offset=offset, excerpt=excerpt,
                             scope=f'分类卡片：已抽取 {len(pages)} 页中的单页片段',
                             extracted_pages=len(pages))
        profile = dict(version=row['version'], evidence_id=eid, page=page, text=excerpt, error='', policy=policy_key)
    except Exception as exc:
        profile = dict(version=row['version'], evidence_id=None, page=None, text='', error=type(exc).__name__, policy=policy_key)
    store.data['paper_profiles'][fid] = profile
    return profile, False


class ProfilesArgs(BaseModel):
    path: str = Field('.', description='待分类的 PDF 目录；维度由用户需求决定，递归枚举可见文件')
    catalog_id: str | None = Field(None, description='翻页沿用首次返回的 C 标识，不重新枚举')
    page: int = Field(1, ge=1)
    evidence: EvidencePolicy | None = Field(None, description='覆盖任务的抽取偏好；同一快照后续翻页沿用首次策略')


@tool('paper_profiles',
      '论文按用户指定标准分类的入口。一次在本地枚举 PDF 并批量返回 16 篇带 F/E 标识的原文卡片，无需先列目录或逐篇 read_file。'
      '结果告知总页数，可在同一轮读取剩余页。必须查看全部页后 submit_classification；证据不足标 uncertain，不按标题猜定论。',
      ProfilesArgs, max_result_chars=16000, read_scope=catalog_scope)
def paper_profiles(a, sb):
    store = _store(sb)
    if a.catalog_id:
        cid = a.catalog_id
        if cid not in store.data['catalogs']:
            raise ToolError('分类目录快照不存在，请重新获取 paper_profiles')
        catalog = store.data['catalogs'][cid]
        if a.evidence is not None and a.evidence.model_dump() != catalog.get('evidence', EvidencePolicy().model_dump()):
            raise ToolError('同一快照不能混用抽取策略，请创建新快照')
    else:
        paths = _pdfs(sb, a.path)
        members = [store.register(p) for p in paths]
        cid = 'C'+uuid.uuid4().hex[:10]
        catalog = dict(path=sb.rel(sb.path(a.path)), members=members,
                       versions={fid:store.data['files'][fid]['version'] for fid in members}, delivered=[],
                       evidence=(a.evidence or evidence_policy(sb)).model_dump(), profiles={})
        store.data['catalogs'][cid] = catalog
    pages = max(1, (len(catalog['members'])+PAGE_SIZE-1)//PAGE_SIZE)
    if a.page > pages:
        raise ToolError(f'共 {pages} 页')
    cards, cached = [], 0
    from agent.sensitive import file_reasons, granted
    policy = EvidencePolicy.model_validate(catalog.get('evidence', {}))
    for fid in catalog['members'][(a.page-1)*PAGE_SIZE:a.page*PAGE_SIZE]:
        path, row = store.resolve(fid, allow_trash=False)
        if row['version'] != catalog['versions'][fid]:
            raise ToolError('目录快照中的文件已变更，请重新获取卡片')
        profile, hit = _profile(sb, fid, policy); cached += int(hit)
        catalog.setdefault('profiles', {})[fid] = profile
        rel = sb.rel(path)
        why = file_reasons(sb, rel, policy.pdf_pages)
        if why and not granted(sb, rel):
            name = json.dumps(row['name'][:90], ensure_ascii=False)[:160]
            cards.append(f"file:{fid} 名称={name}\n[敏感文件：{'；'.join(why)}；未显示内容，分类时 uncertain=true，读取需用户授权]")
            if fid not in catalog['delivered']: catalog['delivered'].append(fid)
            continue
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
    return (f"OK: catalog_id={cid}；范围={catalog['path']}；PDF={len(catalog['members'])}；第 {a.page}/{pages} 页；本页缓存复用={cached}\n"
            '依据用户指定标准及以下原文分类。查看全部页后提交 file_id→category，不复写长路径；不足项 uncertain=true。\n'+'\n'.join(cards))


class Assignment(BaseModel):
    file_id: str
    category: int = Field(ge=1, description='categories 中的序号，从 1 开始')
    uncertain: bool = Field(False, description='片段不足以支持分类标准时必须为 true')
    secondary_categories: list[int] = Field(default_factory=list, max_length=30,
                                           description='可选附加标签；移动仅使用主 category，不复制文件')
    evidence_ids: list[str] | None = Field(None,max_length=5,description='通常省略，自动引用已提供卡片；补读其他页时填写所采用的完整 E 标识')


class SubmitArgs(BaseModel):
    catalog_id: str
    categories: list[str] = Field(min_length=1, max_length=30, description='简短类别名，每项最多55字；标签中的斜杠自动转全角，路径另填 category_paths')
    assignments: list[Assignment] = Field(max_length=500)
    note: str = Field('', max_length=200, description='仅必要说明或用户固定回复标记，不复写完整分类清单')
    dimension: str = Field('用户指定的分类标准', min_length=1, max_length=200)
    base_dir: str | None = Field(None, description='目标根目录；V2 默认沿用来源目录，不能自动增加按方法等层级')
    numbered: bool | None = Field(None, description='类别目录是否编号；V2 默认不编号')
    category_paths: dict[str, str] = Field(default_factory=dict,
        description='类别序号到相对 base_dir 的目录映射，可显式表示多层目录；省略使用类别名')


def classification_markdown(store, pid, plan):
    lines = [f"已形成 {len(plan['categories'])} 类、{len(plan['assignments'])} 篇 PDF 的分类计划，尚未移动文件。计划：`{pid}`。", '',
             '分类标准：'+label(plan.get('dimension', '历史计划'))+'；目标目录：'+label(plan['base_dir'])+'。', '',
             '| 类别 | 文件 | 依据 |', '| --- | --- | --- |']
    for item in plan['assignments']:
        eids=item['evidence_ids']
        ev=store.data['evidence'].get(eids[0],{}) if eids else {}
        evidence = ' '.join(f"[片段{i+1}](evidence:{eid})" for i,eid in enumerate(eids)) if eids else '无可用片段'
        state = '；**待核实**' if item['uncertain'] else '；据片段分类'
        categories = [item['category'], *item.get('secondary_categories', [])]
        names = '、'.join(plan['categories'][n-1] for n in categories)
        lines.append(f"| {label(names)} | {store.file_link(item['file_id'],ev.get('page'))} | {evidence}{state} |")
    lines += ['', '依据范围以各引用片段的抽取记录为准，不代表模型通读全文；分类准确性仍需判断。移动使用主类别，操作前展示完整清单。']
    if plan['note']: lines += ['', label(plan['note'])]
    return '\n'.join(lines)


@tool('submit_classification',
      '保存按用户标准形成的分类计划；dimension、base_dir、numbered、category_paths 表达维度和布局，不能自行改成核心方法。'
      'categories 为类别名列表，assignments 填 F 标识、主类别、可选附加标签与 uncertain。'
      '必须覆盖目录快照每个 PDF 且恰好一次；片段不足时标 uncertain。提交成功后不要再生成长路径清单；不会移动文件。', SubmitArgs, read_scope=catalog_scope)
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
        profile = catalog.get('profiles', {}).get(fid) or store.data['paper_profiles'][fid]
        eids=[e.removeprefix('evidence:') for e in item.evidence_ids] if item.evidence_ids is not None else ([profile['evidence_id']] if profile.get('evidence_id') else [])
        for eid in eids:
            ev=store.resolve_evidence(eid)
            if ev['file_id']!=fid or ev['kind']!='fragment':raise ToolError('分类证据不属于当前文件的原文片段')
        if not eids and not item.uncertain:
            raise ToolError(f'file:{fid} 没有可用正文，必须标 uncertain=true')
        if any(n < 1 or n > len(categories) for n in item.secondary_categories):
            raise ToolError('附加类别序号不正确')
        assignments.append(dict(file_id=fid, category=item.category, uncertain=item.uncertain,evidence_ids=eids,
                                secondary_categories=list(dict.fromkeys(n for n in item.secondary_categories if n != item.category))))
    if set(_pdfs(sb,catalog['path'])) != {store.data['files'][fid]['path'] for fid in ids}:
        raise ToolError('目录文件清单已变化，请重新获取分类快照')
    pid='P'+uuid.uuid4().hex[:10]
    scope=Path(catalog['path'])
    base=a.base_dir if a.base_dir is not None else scope.as_posix()
    numbered = bool(a.numbered)
    _safe_target(sb, base)
    if set(a.category_paths) - {str(i) for i in range(1, len(categories)+1)}:
        raise ToolError('目录映射引用了不存在的类别')
    for rel in a.category_paths.values():
        if not rel or Path(rel).is_absolute() or any(p in {'.', '..'} or p.startswith('.') for p in rel.split('/')) or '\\' in rel:
            raise ToolError('类别目录必须为可见的相对路径，不能穿越目录')
        _safe_target(sb, (Path(base)/rel).as_posix())
    plan=dict(path=catalog['path'], base_dir=base, versions=dict(catalog['versions']),
              categories=categories, assignments=assignments, note=a.note, status='ready',
              dimension=a.dimension, numbered=numbered, category_paths=a.category_paths,
              evidence_policy=catalog.get('evidence', EvidencePolicy().model_dump()))
    directories = [unicodedata.normalize('NFKC', plan_directory(plan, i)).casefold() for i in range(1, len(categories)+1)]
    if len(set(directories)) != len(directories):
        raise ToolError('不同类别映射到了同一目录，请明确互不冲突的 category_paths')
    store.data['classification_plans'][pid]=plan;store._save()
    runtime=getattr(sb,'runtime',None)
    if runtime:
        runtime.capture_output('submit_classification', classification_markdown(store,pid,plan), pid)
    return f'OK: 已保存分类计划 {pid}，共 {len(ids)} 篇；分类表已由本地生成，尚未移动文件。'


def _safe_target(sb, rel):
    parts=Path(rel).parts
    if Path(rel).is_absolute() or any(x.startswith('.') for x in parts if x!='.'):
        raise ToolError('分类目标不能是隐藏或越界路径')
    cursor=sb.root
    for part in parts:
        cursor=cursor/part
        if cursor.is_symlink():
            raise ToolError('分类目标不能经过符号链接')
    return sb.path(rel,write=bool(parts),exact=True)


def _move_plan(sb, pid, plan):
    store=_store(sb);moves=[];targets=set()
    _safe_target(sb,plan['base_dir'])
    for item in plan['assignments']:
        fid=item['file_id'];source,row=store.resolve(fid,allow_trash=False)
        if row['version'] != plan['versions'][fid] or digest(source) != plan['versions'][fid]:
            raise ToolError('分类后文件内容已变化，请重新分类')
        directory=(Path(plan['base_dir']) / plan_directory(plan, item['category'])).as_posix()
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
