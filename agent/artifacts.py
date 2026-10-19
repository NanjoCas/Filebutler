"""Local file identities, versioned evidence and verified execution receipts.

The web layer persists this data per conversation. IDs never grant filesystem
access: every resolution checks the configured sandbox, symlinks and version.
"""
import copy
import hashlib
import json
import logging
import re
import threading
import uuid
from pathlib import Path


def signature(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def label(text):
    return re.sub(r'([\\`*\[\]|<>])', r'\\\1', str(text)).replace('\n', ' ').replace('\r', ' ')


class ArtifactStore:
    def __init__(self, sandbox, data=None, on_change=None):
        self.sb = sandbox
        self.data = copy.deepcopy(data or {})
        if self.data.get('root', str(sandbox.root)) != str(sandbox.root):
            self.data = {}
        self.data['root'] = str(sandbox.root)
        self.data.setdefault('files', {})
        self.data.setdefault('evidence', {})
        self.data.setdefault('receipts', {})
        self.data.setdefault('paper_profiles', {})
        self.data.setdefault('catalogs', {})
        self.data.setdefault('classification_plans', {})
        self.on_change = on_change
        self.lock = threading.RLock()
        self.run_id = ''

    def export(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def _save(self):
        if self.on_change:
            try:
                self.on_change(self.export())
            except Exception as exc:
                # A presentation/database observer must not undo a journalled write.
                logging.getLogger(__name__).warning('文件引用持久化失败：%s', type(exc).__name__)

    def _path(self, rel, trash=False):
        parts = Path(rel).parts
        if not parts or Path(rel).is_absolute() or any(x in ('.', '..') for x in parts):
            raise ValueError('无效文件路径')
        hidden = [x for x in parts if x.startswith('.')]
        if hidden and not (trash and len(parts) == 2 and hidden == ['.trash']):
            raise ValueError('不能引用隐藏文件')
        lexical = self.sb.root / rel
        if any(p.is_symlink() for p in (lexical, *lexical.parents) if p.is_relative_to(self.sb.root)):
            raise ValueError('不能引用符号链接')
        p = self.sb.path(rel, exact=True)
        if not p.is_file():
            raise ValueError('文件已不存在')
        return p

    def register(self, rel):
        p = self._path(rel)
        rel = self.sb.rel(p)
        sig = signature(p)
        with self.lock:
            for fid, row in self.data['files'].items():
                if row['path'] == rel and row['stat'] == sig:
                    return fid
            # A changed file is a new version/identity. Old citations stay stale.
            sha = digest(p)
            if signature(p) != sig:
                raise ValueError('登记期间文件变化，请重试')
            fid = 'F' + uuid.uuid4().hex[:10]
            self.data['files'][fid] = dict(path=rel, name=p.name, stat=sig, version=sha, status='visible')
            self._save()
            return fid

    def resolve(self, fid, allow_trash=True):
        with self.lock:
            row = self.data['files'].get(fid)
            if not row:
                raise ValueError('文件引用不存在或不属于此对话')
            if row['status'] == 'trash' and not allow_trash:
                raise ValueError('文件已移入回收区，仅可通过预览入口查看')
            p = self._path(row['path'], trash=row['status'] == 'trash')
            if signature(p) != row['stat']:
                raise ValueError('文件版本或位置已变化，请重新检索后引用')
            return p, copy.deepcopy(row)

    def relocate(self, src, dst):
        with self.lock:
            changed = False
            for row in self.data['files'].values():
                if row['path'] != src and not row['path'].startswith(src.rstrip('/') + '/'):
                    continue
                target = dst + row['path'][len(src):]
                p = self._path(target, trash=target.startswith('.trash/'))
                # Only journalled renames of the registered contents keep identity.
                if signature(p)[:2] != row['stat'][:2] or digest(p) != row['version']:
                    continue
                row.update(path=target, stat=signature(p), status='trash' if target.startswith('.trash/') else 'visible')
                changed = True
            if changed:
                self._save()

    def evidence(self, fid, kind, *, page=None, offset=0, excerpt='', **facts):
        _, row = self.resolve(fid, allow_trash=False)
        eid = 'E' + uuid.uuid4().hex[:10]
        with self.lock:
            self.data['evidence'][eid] = dict(file_id=fid, version=row['version'], kind=kind,
                                             page=page, offset=offset, excerpt=excerpt[:600], **facts)
            self._save()
        return eid

    def resolve_evidence(self, eid):
        row = self.data['evidence'].get(eid)
        if not row:
            raise ValueError('证据引用不存在')
        _, file = self.resolve(row['file_id'])
        if file['version'] != row['version']:
            raise ValueError('证据版本已过期')
        return copy.deepcopy(row)

    def receipt(self, *, groups, moved, keeps, verified, error='', scan=None):
        rid = 'R' + uuid.uuid4().hex[:10]
        with self.lock:
            self.data['receipts'][rid] = dict(run_id=self.run_id, groups=copy.deepcopy(groups),
                                              moved=copy.deepcopy(moved), keeps=list(keeps),
                                              verified=verified, error=error, scan=copy.deepcopy(scan or {}))
            self._save()
        return rid

    def receipt_markdown(self, rid):
        row = self.data['receipts'][rid]
        if not row['verified']:
            return f"本地执行未全部完成：{label(row['error'])}。已回收 {len(row['moved'])} 份，可尝试撤销。"
        kinds = {'bytes': 'SHA256 相同', 'text': '全文规范化文字相同', 'similar': '经用户确认的近似版本'}
        lines = [f"已将 {len(row['moved'])} 个副本移入回收区，保留 {len(row['keeps'])} 份。", '',
                 '| 判定依据 | 保留文件 | 已回收文件 |', '| --- | --- | --- |']
        for group in row['groups']:
            keep = self.file_link(group['keeper'])
            victims = '、'.join(self.file_link(fid) for fid in group['members'] if fid != group['keeper'])
            lines.append(f"| {kinds[group['kind']]} | {keep} | {victims} |")
        if not row['moved']:
            lines=['本次扫描未生成可回收的确定重复副本。']
        lines += ['', '本地执行后复核通过；结论仅覆盖本次计划。已回收文件可点击预览，也可撤销本轮操作。']
        scan = row.get('scan', {})
        stats = scan.get('stats', {})
        if stats:
            lines += ['', f"扫描范围：{label(scan['path'])}；{stats['files']} 个文件、{stats['pdf']} 个 PDF。"]
            if scan['mode'] == 'text':
                lines += [f"全文抽取失败 {stats['parse_failed']} 份、文字过短或不可用 {stats['short_or_empty']} 份；这些项目不能作全文文字等价判断。"]
            else:
                lines += ['本次按文件字节比较，未进行全文文字等价判断。']
        return '\n'.join(lines)

    def file_link(self, fid, page=None):
        row = self.data['files'][fid]
        return f"[{label(row['name'])}](file:{fid}" + (f'#page={page}' if page else '') + ')'
