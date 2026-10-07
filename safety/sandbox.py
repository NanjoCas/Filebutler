"""沙盒路径校验：所有路径都相对沙盒，resolve 后必须仍在沙盒内。"""
import os
import unicodedata
from pathlib import Path

PROTECTED = {".trash", ".oplog"}  # 内部目录，模型不能写


class SandboxError(Exception):
    pass


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
