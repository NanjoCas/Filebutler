"""Build a reviewable confirmation from validated tool arguments, not terminal text."""
from pathlib import Path
import unicodedata

from safety.sandbox import Sandbox, SandboxError
from tools.fs_read import _fmt_size


def describe_task_value(field, value):
    effects={'create':'创建目录','move':'移动','rename':'改名','trash':'移入回收区'}
    predicates={'within':'限定路径','outside':'排除路径','extensions':'限定扩展名','name_glob':'限定名称',
                'empty_directory':'仅空目录','same_extension':'扩展名不变','max_count':'本轮数量上限',
                'content_unchanged':'内容保持不变'}
    conditions={'path_exists':'路径存在','path_absent':'路径不存在','directory_empty':'目录为空',
                'sha256':'内容哈希一致','tool_success':'已执行指定工具（仍需复核）','manual_review':'人工复核',
                'evidence_valid':'证据引用有效','plan_ready':'计划已保存','plan_applied':'计划执行已核验',
                'receipt_verified':'执行回执有效','file_plan_verified':'文件计划的位置和内容已核验'}
    conditions.update(files_placed='执行前清单中的文件均已按原名归集且内容一致',
                      empty_directories_removed='范围内已无可见空子目录')
    if field=='cancelled': return '已取消' if value else '可继续'
    if field=='goals':
        return '\n'.join(g['description']+'；完成条件：'+ '、'.join(
            conditions.get(c['kind'],c['kind']) + (' '+c['path'] if c.get('path') else '')
            + (' → '+c['destination'] if c.get('destination') else '')
            + ('；后缀：'+ '、'.join(c['extensions']) if c.get('extensions') else '')
            + ('；排除：'+ '、'.join(c['exclude_paths']) if c.get('exclude_paths') else '')
            + ('；仅当前层' if c.get('recursive') is False else '')
            + (' '+c['tool'] if c.get('tool') else '') + (' '+c['reference'] if c.get('reference') else '')
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


def build_confirmation(request: dict, sb: Sandbox) -> dict:
    tool, args = request["tool"], request["args"]
    groups, notes = [], []

    def group(key, title, paths, tone="neutral", planned=False):
        items = [describe_path(sb, str(p), planned=planned) for p in paths]
        result = {"key": key, "title": title, "tone": tone, "items": items}
        groups.append(result)
        return items

    if tool == 'task_edit':
        changed=[]
        title,label,tone='确认修改任务','应用此版本','warning'
        summary=f"任务 {args['task_id']} · 当前版本 {args['revision']}，将修改 {len(args['changes'])} 项设置。"
        names={'goals':'目标与完成条件','constraints':'约束','read_roots':'读取范围',
               'write_roots':'写入范围','allowed_effects':'允许操作','unresolved_questions':'待确认问题'}
        for change in args['changes']:
            notes.append(names.get(change['field'],change['field'])+'：\n修改前：'+
                         describe_task_value(change['field'],change['before'])+'\n修改后：'+
                         describe_task_value(change['field'],change['after']))
        notes += ['原因：'+args['reason'], '本次仅修改任务；不会直接操作文件，旧版本的复核结果将失效。']
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
        summary=f"计划 {args['plan_id']}：将执行 {len(changed)} 项操作。"
        notes += ['本次确认绑定此操作清单；来源或目标变化会停止执行。',
                  '计划摘要：'+args['digest'], '已完成操作有日志，可按运行撤销；执行中断时请先查看具体原因。']
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
                groups.append({"key": g["id"], "title": f"重复组 {index} · {g['id']}", "tone": "neutral",
                               "description": kinds.get(g.get("kind"), ""), "items": items})
        else:
            # Do not guess family membership by list order when handling an older request.
            items = [dict(describe_path(sb, p), disposition="retain") for p in retained]
            items += [dict(describe_path(sb, p), disposition="trash") for p in affected]
            groups.append({"key": "unmatched", "title": "本次操作涉及的文件", "tone": "neutral", "items": items})
            notes.append("此请求未提供完整重复组关系，请逐项核对。")
        title, label, tone = "确认清理重复文件", "确认移入回收区", "warning"
        summary = f"将 {len(changed)} 项移入回收区，保留 {len(retained)} 项；请按重复组核对。"
        notes.append("移入回收区后可通过本轮撤销恢复。")
    elif tool == "trash":
        changed = group("affected", "将移入回收区", [args["path"]], "warning")
        changed[0]["disposition"] = "trash"
        title, label, tone = "确认移入回收区", "确认移入回收区", "warning"
        summary = "这一步会将以下项目移入回收区，可在本轮结束后撤销。"
        if changed[0]["kind"] == "directory":
            notes.append("此操作包含文件夹内的全部文件和子文件夹，可点击文件夹查看内容。")
    elif tool == "move_many":
        changed=[]
        destinations={}
        for move in args['moves']:
            item=describe_path(sb,move['src'])
            item['destination']=move['dst']
            if move.get('uncertain'):item['message']='分类结果待核实，请在移动前查看片段或原文件'
            destinations.setdefault(move['dst_dir'],[]).append(item)
            changed.append(item)
        for index,(destination,items) in enumerate(destinations.items(),1):
            groups.append({'key':f'category_{index}','title':destination,'tone':'neutral','items':items})
        title,label,tone='确认执行分类计划','确认批量移动','neutral'
        summary=f'按 {len(groups)} 个目标分类移动 {len(changed)} 篇，保留原文件名；确认后统一创建缺少的目录。'
        notes.append('请逐类查看来源和目标；待核实项仍会标注。每个已完成操作都有日志，本轮结束后可撤销。')
    elif tool == "move":
        changed = group("affected", "将移动", [args["src"]])
        destination = group("destination", "目标文件夹", [args["dst_dir"]])
        changed[0]["destination"] = (Path(destination[0]["path"]) / changed[0]["name"]).as_posix()
        title, label, tone = "确认移动文件", "确认移动", "neutral"
        summary = "保留文件名，将以下项目移动到目标文件夹。"
        if changed[0]["kind"] == "directory":
            notes.append("移动文件夹时，其全部内容会随之移动。")
    elif tool == "rename":
        changed = group("affected", "将重命名", [args["path"]])
        new_name = unicodedata.normalize("NFC", args["new_name"].strip())
        changed[0]["destination"] = (Path(changed[0]["path"]).parent / new_name).as_posix()
        title, label, tone = "确认重命名", "确认重命名", "neutral"
        summary = "文件仍在原文件夹中，名称将按下方所示更改。"
        before, after = Path(changed[0]["path"]).suffix, Path(new_name).suffix
        if before.lower() != after.lower():
            notes.append(f"扩展名将从 {before or '无'} 更改为 {after or '无'}。")
    elif tool == "make_dir":
        changed = group("affected", "将创建文件夹", [args["path"]], planned=True)
        title, label, tone = "确认创建文件夹", "确认创建", "neutral"
        summary = "将在以下位置创建文件夹，尚未创建的位置暂时无法预览。"
    else:
        changed = []
        title, label, tone = "确认文件操作", "确认执行", "neutral"
        summary = "请检查操作详情后再确认。"
        notes.append(f"操作类型：{tool}")

    return {"operation": tool, "title": title, "summary": summary, "confirm_label": label, "tone": tone,
            "counts": {"affected": len(changed), "retained": len(args.get("保留", []))},
            "groups": groups, "notes": notes}
