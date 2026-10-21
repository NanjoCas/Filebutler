"""Conservative, whole-instruction routes for deterministic local work.

These patterns are command forms, not keyword authorization. Constraints,
negation, quoted instructions and compound tasks fall back to the agent.
"""
import re


def local_task(instruction):
    text = instruction.strip().rstrip('。！？?! ')
    text = re.sub(r'^(?:请|帮我)', '', text, count=1)
    path = '.'
    scope = re.match(r'^在「([^」\r\n]+)」(?:目录|文件夹)?(?:中|里)[，,\s]*', text)
    if scope:
        path, text = scope.group(1), text[scope.end():]
    # Only separators are normalized; no words or qualifications are discarded.
    text = re.sub(r'[，,\s]', '', text)
    if re.fullmatch(r'(?:数一下|统计)(?:所有)?(?:PDF|pdf)(?:文件)?(?:数量)?|(?:一共|总共)?有多少(?:篇论文|个PDF|个pdf)', text):
        return 'inventory', 'inventory_files', dict(path=path, ext='pdf')
    if text in {'文件统计', '统计文件数量', '统计文件类型和大小', '统计文件类型和数量', '一共有多少个文件', '有多少个文件'}:
        return 'inventory', 'inventory_files', dict(path=path)
    scans = {'检查是否有完全相同的文件': 'bytes', '查找字节完全相同的文件': 'bytes',
             '查找正文相同的PDF': 'text', '查找正文相同的pdf': 'text'}
    if text in scans:
        return 'duplicate_scan', 'find_duplicates', dict(path=path, mode=scans[text])
    cleanups = {'清理字节完全相同的文件每组只留一份': 'bytes',
                '清理正文相同的PDF每组只留一份': 'text',
                '清理正文相同的pdf每组只留一份': 'text'}
    if text in cleanups:
        return 'duplicate_cleanup', 'deduplicate', dict(path=path, mode=cleanups[text])
    return None


def duplicate_summary(sb):
    """Render scan facts locally; never infer semantic or visual equivalence."""
    from agent.artifacts import label
    scans = getattr(sb, '_dedup_scans', {})
    scan = next(reversed(scans.values()))
    groups = scan['groups']
    stats = scan['stats']
    basis = '字节完全相同' if scan['mode'] == 'bytes' else '字节或规范化提取文本相同'
    lines = [f"扫描 {stats['files']} 个文件，发现 {len(groups)} 组{basis}的文件；尚未移动文件。"]
    if groups:
        lines += ['', '| 组 | 文件数 | 建议保留 |', '| --- | ---: | --- |']
        store = getattr(sb, 'artifacts', None)
        for group in groups[:100]:
            keeper = group['keep']
            link = store.file_link(store.register(keeper)) if store else label(keeper)
            lines.append(f"| {group['id']} | {len(group['members'])} | {link} |")
        if len(groups) > 100:
            lines += ['', f'这里只展示前 100 组；总组数已包含全部结果，请缩小目录范围查看剩余组。']
    if scan['mode'] != 'bytes':
        lines += ['', f"全文解析失败 {stats['parse_failed']} 篇，文字过短或无可用文字 {stats['short_or_empty']} 篇。提取文本相同不证明图片、表格或版式相同；未进行近似版本合并。"]
    return '\n'.join(lines)
