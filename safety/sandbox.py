"""沙盒路径校验：所有路径都相对沙盒，resolve 后必须仍在沙盒内。"""
import json
import os
import unicodedata
from pathlib import Path

PROTECTED = {".trash", ".oplog"}  # 内部目录，模型不能写


class SandboxError(Exception):
    pass


class LibraryMissing(SandboxError):
    """配置的资料库不可用；说明写给用户。"""


def open_library(root, known_file) -> Path:
    """检查配置的资料库（用户决定 2026-10-29，D 组第 1 项）。

    Sandbox() 会自动建出不存在的目录：路径写错、资料库被移走或改名时，界面上就成了一个
    空资料库，像是"文件全没了"。所以从配置进来的路径先过这里：
    - 存在：记为用过的资料库；
    - 不存在、以前用过：不新建，说明可能被移动、改名或硬盘没连接；
    - 不存在、从没用过（第一次使用）：只建最后一级目录；上级目录也不存在（硬盘没插、
      路径写错）时不建，说明原因。
    ``known_file`` 是记录用过的资料库的 JSON 文件。"""
    root, known_file = Path(root).resolve(), Path(known_file)
    try:
        known = set(json.loads(known_file.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        known = set()
    if root.exists() and not root.is_dir():
        raise LibraryMissing(f"资料库路径 {root} 不是文件夹，请在 config.yaml 里改成资料库所在的文件夹")
    if not root.exists():
        if str(root) in known:
            raise LibraryMissing(f"找不到资料库 {root}：它之前在这里，可能被移动、改名，或所在的硬盘没有连接。"
                                 "FileButler 没有新建空文件夹；请找回它，或在 config.yaml 里改成新的位置")
        if not root.parent.is_dir():
            raise LibraryMissing(f"找不到资料库 {root}：上级文件夹 {root.parent} 也不存在，可能路径写错，或所在的硬盘没有连接。"
                                 "FileButler 没有新建文件夹；请检查 config.yaml 里的路径")
        try:
            root.mkdir()
        except OSError as exc:
            raise LibraryMissing(f"无法新建资料库 {root}：{exc.strerror or exc}") from exc
    if str(root) not in known:
        try:
            known_file.parent.mkdir(parents=True, exist_ok=True)
            known_file.write_text(json.dumps(sorted(known | {str(root)}), ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 记不下来时下次仍按第一次使用处理，不影响本次
    return root


class Sandbox:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.violations = 0  # 越权尝试次数

    def path(self, p: str, write: bool = False, exact: bool = False) -> Path:
        """把模型给的相对路径转成真实路径；越界或写受保护位置时抛 SandboxError。
        模型给的路径会做容错（NFC、首尾空格、换成磁盘上的真实名字）；
        exact=True 用于 undo：日志里记的就是准确名字，只做安全检查，不做任何名字转换。"""
        p = str(p) if exact else unicodedata.normalize("NFC", str(p))
        if not p.strip():
            p = "."
        # resolve() 会展开 .. 和符号链接；绝对路径拼接后会直接替换 root，同样被拦下
        full = (self.root / p).resolve()
        if not full.is_relative_to(self.root):
            self._deny("只能操作沙盒内的文件")
        if not exact and not full.exists():
            full = self._match_spaces(full)  # 只在沙盒内找，同一文件夹里的结果也必然在沙盒内
        if not exact and full.exists():
            full = self._real_name(full)
        if write:
            parts = full.relative_to(self.root).parts
            if not parts:
                self._deny("不能对沙盒根目录本身做写操作")
            if parts[0] in PROTECTED:
                self._deny(f"{parts[0]} 是内部目录，不能写")
        return full

    @staticmethod
    def _match_spaces(full: Path) -> Path:
        """真实文件名可能带首尾空格（如“ Exploiting….pdf”），模型调用时常把空格丢掉，反过来也可能多带。
        原样找不到时，在同一文件夹里找去掉首尾空格后同名的文件，恰好一个才采用。"""
        want = full.name.strip()
        if not full.parent.is_dir() or not want:
            return full
        hits = [c for c in full.parent.iterdir() if c.name.strip() == want]
        return hits[0] if len(hits) == 1 else full

    def _real_name(self, full: Path) -> Path:
        """换成磁盘上的真实名字。macOS 默认不区分大小写，还会做 Unicode 折叠：
        “report.pdf”能打开“Report.PDF”，“ff”能打开连字“ﬀ”，NFC 能打开 NFD。不换的话，
        move 会把文件悄悄改成模型写的名字，日志也记错，undo 就还原不回原名。"""
        cur = self.root
        for part in full.relative_to(self.root).parts:
            names = os.listdir(cur)
            if part not in names:
                part = next((n for n in names if os.path.samefile(cur / n, cur / part)), part)
            cur = cur / part
        return cur

    def rel(self, full: Path) -> str:
        r = full.relative_to(self.root).as_posix()
        return r if r != "." else "."

    def _deny(self, why: str):
        self.violations += 1
        raise SandboxError(f"拒绝：{why}")


def is_hidden(path: Path) -> bool:
    """.DS_Store、.trash、.oplog 等点开头的文件一律对模型隐藏。"""
    return path.name.startswith(".")
