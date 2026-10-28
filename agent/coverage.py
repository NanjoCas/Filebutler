"""Search coverage ledger (routing redesign S7, 9 and 9.1). Pure functions over plain dicts.

A ledger is keyed by request, scope, enumeration snapshot and index revision. Each
file entry binds its content version and extraction version to a status:

    reviewed     read with read_file, or cited by report_findings, at this version
    excluded     not readable as text here (unsupported, failed, empty, OCR pending), with why
    unreviewed   readable and not reviewed; the search only knows whether its terms matched

A later scan of the same scope with a different snapshot replaces the ledger: files
whose version did not change keep their review (``inherited_from`` names the old
snapshot); new and changed files start unreviewed and are listed, so an old 10/10
is never reported as 10/11 complete. Nothing here reads a file or calls a model.
"""
import copy
import time

EXCLUDED = {'unsupported': '格式不支持', 'failed': '读取失败', 'empty': '没有可读文字', 'pending_ocr': '扫描页尚未识别'}


def _entry(row, inherited=None):
    state = row['state']
    status = 'excluded' if state in EXCLUDED else 'unreviewed'
    entry = dict(version=row['version'], state=state, status=status, reason=EXCLUDED.get(state, ''))
    if inherited:
        entry.update(status=inherited['status'], reason=inherited['reason'], inherited_from=inherited['snapshot'])
    return entry


def record_scan(ledgers, scan, request_id):
    """Add one search_content coverage summary; returns the updated ledgers (scope -> ledger)."""
    ledgers = copy.deepcopy(ledgers or {})
    scope = scan['scope']
    old = ledgers.get(scope)
    key = dict(request=request_id, scope=scope, snapshot=scan['snapshot_id'], index_revision=scan['index_revision'])
    if old and old['key'] == key:
        ledger = old
        # OCR can finish without changing the source's size or mtime. Refresh
        # extraction states even when the enumeration snapshot stays identical.
        ledger['files'] = {rel: old['files'][rel] if rel in old['files'] and
                          old['files'][rel]['version'] == row['version'] and old['files'][rel]['state'] == row['state']
                          else _entry(row) for rel, row in scan['files'].items()}
    else:
        files, changes = {}, dict(added=[], changed=[], removed=[])
        for rel, row in scan['files'].items():
            before = (old or {}).get('files', {}).get(rel)
            same = before and before['version'] == row['version'] and old['key']['index_revision'] == key['index_revision']
            inherited = dict(status=before['status'], reason=before['reason'],
                             snapshot=before.get('inherited_from') or old['key']['snapshot']) \
                if same and before['status'] == 'reviewed' and before['state'] == row['state'] and old['key']['request'] == request_id else None
            files[rel] = _entry(row, inherited)
            if same and before['state'] == row['state']:
                previous = before.get('previous_review')
                if before['status'] == 'reviewed' and old['key']['request'] != request_id:
                    previous = dict(request=old['key']['request'], reason=before['reason'], snapshot=old['key']['snapshot'])
                if previous:
                    files[rel]['previous_review'] = previous
            if old and not before: changes['added'].append(rel)
            elif old and not same: changes['changed'].append(rel)
        if old:
            changes['removed'] = sorted(set(old['files']) - set(scan['files']))
        ledger = dict(key=key, files=files, scans=[], candidates=[], truncated=False,
                      changes=changes if old and any(changes.values()) else None, created=time.time())
    ledger['scans'] = (ledger['scans'] + [scan['scan_id']])[-20:]
    ledger['candidates'] = sorted(set(ledger['candidates']) | set(scan['candidates']))
    ledger['truncated'] = ledger['truncated'] or scan['candidate_truncated']
    ledger['matched_files'] = max(ledger.get('matched_files', 0), scan['matched_files'])
    ledger['pending_ocr_units'] = scan['pending_ocr_units']
    ledger['ocr_failed_units'] = scan['ocr_failed_units']
    ledger['enumeration_complete'] = scan['enumeration_complete']
    ledgers[scope] = ledger
    return ledgers


def _version(sb, rel):
    try:
        st = sb.path(rel).stat()
    except Exception:
        return None
    return [st.st_size, st.st_mtime_ns]


def record_review(ledgers, sb, paths, reason):
    """Mark files reviewed in every ledger that lists them at their current version."""
    if not ledgers:
        return ledgers
    ledgers = copy.deepcopy(ledgers)
    for raw in paths:
        try:
            rel = sb.rel(sb.path(raw))
        except Exception:
            continue
        version = _version(sb, rel)
        for ledger in ledgers.values():
            entry = ledger['files'].get(rel)
            if entry and entry['version'] == version and entry['status'] != 'excluded':
                entry.update(status='reviewed', reason=reason)
                entry.pop('inherited_from', None)
    return ledgers


def summary(ledger):
    """Counts of one ledger: total readable, reviewed, excluded, unreviewed, and gaps."""
    files = ledger['files'].values()
    readable = [f for f in files if f['state'] != 'unsupported']
    count = lambda status: sum(f['status'] == status for f in readable)
    changes = ledger.get('changes') or {}
    pending_new = [rel for rel in changes.get('added', []) + changes.get('changed', [])
                   if ledger['files'].get(rel, {}).get('status') == 'unreviewed']
    return dict(scope=ledger['key']['scope'], total=len(readable), reviewed=count('reviewed'), excluded=count('excluded'),
                previously_read=sum('previous_review' in f for f in readable),
                unreviewed=count('unreviewed'), inherited=sum('inherited_from' in f for f in readable),
                truncated=ledger['truncated'], matched=ledger.get('matched_files', 0),
                returned=len(ledger['candidates']), pending_ocr=ledger.get('pending_ocr_units', 0),
                ocr_failed=ledger.get('ocr_failed_units', 0), enumeration_complete=ledger.get('enumeration_complete', True),
                changed=pending_new, removed=changes.get('removed', []))


def gap_line(s):
    """One sentence for the main answer when a coverage gap is known locally, else ''."""
    gaps = []
    if not s['enumeration_complete']: gaps.append('有文件夹无法列出')
    if s['pending_ocr']: gaps.append(f"{s['pending_ocr']} 个扫描页或图片尚未识别")
    if s['ocr_failed']: gaps.append(f"{s['ocr_failed']} 个扫描页或图片识别失败")
    if s['changed']: gaps.append(f"资料库在检索之后有变化，{len(s['changed'])} 个新增或变更的文件尚未核对")
    if s['truncated']: gaps.append(f"有词项重合 {s['matched']} 个，只看了前 {s['returned']} 个候选")
    if not gaps:
        return ''
    scope = '整个资料库' if s['scope'] in ('.', '') else s['scope']
    return (f"检索覆盖（{scope}）：可读文件 {s['total']} 个，已核对 {s['reviewed']} 个，其余未核对；"
            + '；'.join(gaps) + '。不代表已经找全。')


def table(ledgers):
    """Markdown for the details: one row per scope."""
    rows = ['| 范围 | 可读文件 | 已核对 | 未核对 | 排除 | 说明 |', '| --- | --- | --- | --- | --- | --- |']
    for ledger in ledgers.values():
        s = summary(ledger)
        note = []
        if s['inherited']: note.append(f"{s['inherited']} 个沿用上次核对（内容未变）")
        if s['previously_read']: note.append(f"{s['previously_read']} 个以前读过，不计入本次已核对")
        if s['changed']: note.append(f"{len(s['changed'])} 个新增或变更未核对")
        if s['truncated']: note.append(f"候选截断：重合 {s['matched']}、返回 {s['returned']}")
        if s['pending_ocr']: note.append(f"{s['pending_ocr']} 页待识别")
        rows.append(f"| {s['scope']} | {s['total']} | {s['reviewed']} | {s['unreviewed']} | {s['excluded']} | {'；'.join(note) or '—'} |")
    return '\n'.join(rows)
