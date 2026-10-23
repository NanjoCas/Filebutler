"""Build a reviewable confirmation from validated tool arguments, not terminal text."""
from pathlib import Path

from safety import names as safe_names
from safety.sandbox import Sandbox, SandboxError
from tools.fs_read import _fmt_size


def describe_task_value(field, value):
    effects={'create':'创建目录','move':'移动','rename':'改名','trash':'移入回收区'}
    predicates={'within':'限定路径','outside':'排除路径','extensions':'限定扩展名','max_count':'本轮数量上限'}
    conditions={'manual_review':'人工复核','evidence_valid':'证据引用有效','plan_ready':'计划已保存',
                'plan_applied':'计划执行已核验','receipt_verified':'执行回执有效','file_plan_verified':'文件计划的位置和内容已核验'}
    if field=='cancelled': return '已取消' if value else '可继续'
    if field=='goals':
        return '\n'.join(g['description']+'；完成条件：'+ '、'.join(
            conditions.get(c['kind'],c['kind']) + (' '+c['reference'] if c.get('reference') else '')
            for c in g['conditions']) for g in value) or '无'
    if field=='constraints':
        lines=[]
        for c in value:
            line=c['description']
            if c['forbidden_effects']: line+='；禁止：'+'、'.join(effects.get(x,x) for x in c['forbidden_effects'])
            for r in c.get('rules',[]):
                line+='；'+predicates.get(r['predicate'],r['predicate'])
                line+='（'+('源对象' if r['selector']=='source' else '目标对象')+'；'+'、'.join(effects.get(x,x) for x in r['effects'])+'）'
                if r['values']: line+='：'+'、'.join(r['values'])
                if r['limit'] is not None: line+='：'+str(r['limit'])
            lines.append(line)
        return '\n'.join(lines) or '无'
    if field=='allowed_effects': return '、'.join(effects.get(x,x) for x in value) or '无'
    return '\n'.join(value) or '无'


def describe_path(sb: Sandbox, value: str, *, planned: bool = False) -> dict:
    item = {"path": value, "name": Path(value).name or "沙盒根目录", "kind": "directory" if planned else "unknown", "planned": planned,
            "previewable": False, "size_text": "", "message": ""}
    try:
        if Path(value).is_absolute() or any(p.startswith(".") for p in Path(value).parts if p != "."):
            raise ValueError("路径不可预览")
        cursor = sb.root
        for part in Path(value).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("符号链接不可预览")
        path = sb.path(value)
        if any(p.startswith(".") for p in path.relative_to(sb.root).parts):
            raise ValueError("内部文件不可预览")
        item.update(path=sb.rel(path), name=path.name if path != sb.root else "沙盒根目录")
        if not path.exists():
            item["message"] = "将在确认后创建" if planned else "文件不存在，无法预览"
            return item
        item["kind"] = "directory" if path.is_dir() else "file"
        item["previewable"] = path.is_dir() or path.is_file()
        item["size_text"] = "文件夹" if path.is_dir() else _fmt_size(path.stat().st_size)
        if planned:
            item["message"] = "文件夹已存在，无需重复创建" if path.is_dir() else "此处已有同名文件，无法创建文件夹"
    except (SandboxError, ValueError, OSError):
        item["message"] = "路径不可访问，无法预览"
    return item


def _hidden_label(sb, folder, rel, note=''):
    """Name and size of a hidden or system file (never previewable); a folder shows its file count."""
    try:
        path = sb.root / folder / rel.rstrip('/')
        if path.is_dir():
            files = [p for p in path.rglob('*') if p.is_file()]
            text = f"{rel}（文件夹，{len(files)} 个文件，{_fmt_size(sum(p.stat().st_size for p in files))}）"
        else:
            text = f"{rel}（{_fmt_size(path.stat().st_size)}）"
    except OSError:
        text = rel
    return text + (f"——{note}" if note else '')


def build_confirmation(request: dict, sb: Sandbox) -> dict:
    """The card the user decides on. Like a permission prompt: one line of what will
    happen (headline), the few things that need attention (warnings), and the rest
    folded into details. title/summary/groups/notes stay for the terminal and the
    full review dialog."""
    card = _mark_names(_build_confirmation(request, sb), sb)
    limits = card.pop("limit_notes")
    severity = lambda n: 0 if n.startswith("⚠ 以下名称") else 1 if n.startswith("⚠⚠") else 2
    card["warnings"] = sorted((n for n in card["notes"] if n.startswith("⚠")), key=severity) + limits
    card["details"] = [n for n in card["notes"] if not n.startswith("⚠") and n not in limits]
    return card


def _place(path):
    parent = Path(path).parent.as_posix()
    return "根目录" if parent in ("", ".") else parent


def _headline(tool, args):
    """What will happen, in one line, without internal identifiers."""
    if tool == "file_plan":
        steps = [s for s in args["steps"] if s["effect"] != "create"]
        folders = {f["source"] for f in args.get("folders") or []}
        unit = lambda rows: "项" if any(r["source"] in folders for r in rows) else "个文件"
        moves = [s for s in steps if s["effect"] == "move"]
        renames = [s for s in steps if s["effect"] == "rename"]
        trashes = [s for s in steps if s["effect"] == "trash"]
        parts = []
        if len(moves) == 1:
            parts.append(f"把「{Path(moves[0]['source']).name}」移到「{_place(moves[0]['destination'])}」")
        elif moves:
            places = {_place(s["destination"]) for s in moves}
            parts.append(f"移动 {len(moves)} {unit(moves)}" + (f"到「{places.pop()}」" if len(places) == 1 else ""))
        if len(renames) == 1:
            parts.append(f"把「{Path(renames[0]['source']).name}」改名为「{Path(renames[0]['destination']).name}」")
        elif renames:
            parts.append(f"改名 {len(renames)} {unit(renames)}")
        if trashes:
            parts.append(f"把 {len(trashes)} {unit(trashes)}移入回收区")
        return "，".join(parts) + "（可撤销）" if parts else "新建文件夹"
    if tool == "task_edit":
        return f"修改任务要求（{len(args['changes'])} 处）"
    if tool == "trash_many":
        return f"清理重复文件：{len(args.get('移入回收区', []))} 项移入回收区，保留 {len(args.get('保留', []))} 项（可撤销）"
    return "执行一项文件操作"


def _mark_names(card: dict, sb: Sandbox) -> dict:
    """Mark invisible and bidi characters wherever the user reads a name. item["path"]
    stays raw for previews; the text shown for it is item["path_text"]."""
    items = [item for g in card["groups"] for item in g["items"]]
    # Names to mark whole inside notes (where newlines are layout): planned names
    # from the items, which may not exist yet, and every such name on disk.
    raw = {part for item in items for value in (item["path"], item.get("destination") or "")
           for part in Path(value).parts if safe_names.VISIBLE.search(part)}
    raw |= {n for n in safe_names.unsafe_names(sb.root) if safe_names.VISIBLE.search(n)}
    ordered = sorted(raw, key=len, reverse=True)

    def shown(text, multiline=False):
        for name in ordered:
            text = text.replace(name, safe_names.mark(name))
        return safe_names.mark(text, keep_newlines=multiline)

    flagged = set()
    for item in items:
        flagged |= {p for p in Path(item["path"]).parts + Path(item.get("destination") or "").parts if p in raw}
        for key in ("name", "destination", "message"):
            if item.get(key):
                item[key] = shown(item[key])
        item["path_text"] = shown(item["path"])
    for g in card["groups"]:
        g["title"] = shown(g["title"])
        if g.get("description"):
            g["description"] = shown(g["description"])
    flagged |= {n for n in raw if any(n in text for text in card["notes"] + [card["summary"], card["headline"]])}
    card["summary"] = shown(card["summary"])
    card["headline"] = shown(card["headline"])
    card["limit_notes"] = [shown(n, multiline=True) for n in card["limit_notes"]]
    card["notes"] = [shown(n, multiline=True) for n in card["notes"]]
    if flagged:
        card["notes"].insert(0, "⚠ 以下名称含不可见字符或会改变显示顺序的字符，已用 ⟦⟧ 标出，请核对后再确认："
                             + "、".join(safe_names.mark(n) + (f"（实际扩展名 {Path(n).suffix}）" if Path(n).suffix else "")
                                        for n in sorted(flagged)) + "。")
    return card


def _build_confirmation(request: dict, sb: Sandbox) -> dict:
    tool, args = request["tool"], request["args"]
    groups, notes = [], []

    if tool == 'task_edit':
        changed=[]
        title,label,tone='确认修改任务','应用此版本','warning'
        summary=f"将修改任务的 {len(args['changes'])} 项要求。"
        names={'goals':'目标与完成条件','constraints':'约束','read_roots':'读取范围',
               'write_roots':'写入范围','allowed_effects':'允许操作','unresolved_questions':'待确认问题'}
        for change in args['changes']:
            notes.append(names.get(change['field'],change['field'])+'：\n修改前：'+
                         describe_task_value(change['field'],change['before'])+'\n修改后：'+
                         describe_task_value(change['field'],change['after']))
        notes += ['原因：'+args['reason'], '只修改任务要求，不会操作文件。']
    elif tool == 'file_plan':
        changed=[]
        names={'create':'创建目录','move':'移动','rename':'改名','trash':'移入回收区'}
        for index, step in enumerate(args['steps']):
            path=step['source'] or step['destination']
            item=describe_path(sb,path,planned=step['effect']=='create')
            if step['effect']=='trash': item['disposition']='trash'
            else: item['destination']=step['destination']
            groups.append(dict(key=f'action_{index}',title=names[step['effect']],tone='neutral',items=[item]))
            changed.append(item)
        title,label,tone='确认执行文件计划','确认执行此计划','neutral'
        summary=f"将执行 {len(changed)} 项操作。"
        for row in args.get('hidden_contents') or []:
            if row['metadata']:
                notes.append(f"回收 {row['folder']} 时，将连同系统生成的文件一起移入回收区："
                             + '、'.join(_hidden_label(sb, row['folder'], p, row.get('notes', {}).get(p, '')) for p in row['metadata'])
                             + '（可撤销）。')
            if row['hidden']:
                names = '、'.join(_hidden_label(sb, row['folder'], p, row.get('notes', {}).get(p, '')) for p in row['hidden'])
                if row.get('consent_source') == 'option':
                    notes.append(f"含你已同意回收的隐藏文件：{names}（可撤销）。")
                elif row.get('consent_source') == 'text':
                    notes.append(f"⚠ 你在对话中以文字回复，请在此确认：{row['folder']} 将连同隐藏文件 {names} 一起移入回收区（可撤销）。")
                else:
                    notes.append(f"⚠ {row['folder']} 含非系统隐藏文件：{names}。它们可能是配置或密钥，确认后将一起移入回收区（可撤销）。")
            if row.get('sensitive'):
                from agent.secrets_scan import describe
                notes.append('⚠⚠ 本地检查发现疑似密钥，建议保留：'
                             + '；'.join(describe(f"{row['folder']}/{rel}", r, masked=True) for rel, r in row['sensitive'].items()) + '。')
        if args.get('new_folders'):
            notes.insert(0, '⚠ 将新建文件夹：' + '、'.join(args['new_folders']) + '。不是你想要的位置就拒绝，拒绝后不执行任何操作。')
        for row in args.get('folders') or []:
            sample = '、'.join(row['files'][:5]) + ('等' if len(row['files']) > 5 else '')
            notes.append(f"整体移动文件夹 {row['source']} → {row['destination']}：含 {len(row['files'])} 个文件"
                         + (f"（{sample}）" if row['files'] else '') + "，执行后逐个核对内容。")
        for row in args.get('overwrites') or []:
            if row.get('consent_source') == 'text':
                notes.append(f"⚠ 你在对话中以文字回复，请在此确认：覆盖 {row['target']}，原有文件移入回收区，再放入新文件（可撤销）。")
            else:
                notes.append(f"⚠ 覆盖 {row['target']}（你已选择）：原有文件移入回收区，再放入新文件（可撤销）。")
        extra = args.get('scope_additions') or {}
        if extra.get('write_roots'):
            notes.append('这些位置不在你最初说的范围内：'+'、'.join(extra['write_roots']))
    elif tool == "trash_many":
        affected, retained = args.get("移入回收区", []), args.get("保留", [])
        changed = [describe_path(sb, p) for p in affected]
        review_groups = request.get("groups") or []
        members = [p for g in review_groups for p in g["members"]]
        valid_groups = (bool(review_groups) and len(members) == len(set(members))
                        and set(members) == set(affected) | set(retained)
                        and all(g["keeper"] in retained and g["keeper"] in g["members"]
                                and len(g["members"]) >= 2
                                and all(p in affected for p in g["members"] if p != g["keeper"]) for g in review_groups))
        kinds = {"bytes": "字节完全相同", "text": "提取文字相同", "similar": "近似版本，请核对差异"}
        if valid_groups:
            for index, g in enumerate(review_groups, 1):
                ordered_members = [g["keeper"]] + [p for p in g["members"] if p != g["keeper"]]
                items = [describe_path(sb, p) for p in ordered_members]
                for item, source in zip(items, ordered_members):
                    item["disposition"] = "retain" if source == g["keeper"] else "trash"
                groups.append({"key": g["id"], "title": f"重复组 {index}", "tone": "neutral",
                               "description": kinds.get(g.get("kind"), ""), "items": items})
        else:
            # Do not guess family membership by list order when handling an older request.
            items = [dict(describe_path(sb, p), disposition="retain") for p in retained]
            items += [dict(describe_path(sb, p), disposition="trash") for p in affected]
            groups.append({"key": "unmatched", "title": "本次操作涉及的文件", "tone": "neutral", "items": items})
            notes.append("无法按重复组归类，请逐项核对。")
        title, label, tone = "确认清理重复文件", "确认移入回收区", "warning"
        summary = f"将 {len(changed)} 项移入回收区，保留 {len(retained)} 项；请按重复组核对。"
    else:
        changed = []
        title, label, tone = "确认文件操作", "确认执行", "neutral"
        summary = "请检查操作详情后再确认。"

    # Limits the user stated that no local check can verify: shown with the warnings.
    limit_notes = list(request.get("notes") or [])
    return {"operation": tool, "title": title, "summary": summary, "confirm_label": label, "tone": tone,
            "headline": _headline(tool, args),
            "counts": {"affected": len(changed), "retained": len(args.get("保留", []))},
            "groups": groups, "notes": notes + limit_notes, "limit_notes": limit_notes}
