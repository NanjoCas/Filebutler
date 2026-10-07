"""沙盒路径校验：所有路径都相对沙盒，resolve 后必须仍在沙盒内。"""
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

    def path(self, p: str, write: bool = False) -> Path:
        """把模型给的相对路径转成真实路径；越界或写受保护位置时抛 SandboxError。"""
        p = unicodedata.normalize("NFC", str(p))
        if not p.strip():
            p = "."
        # resolve() 会展开 .. 和符号链接；绝对路径拼接后会直接替换 root，同样被拦下
        full = (self.root / p).resolve()
        if not full.is_relative_to(self.root):
            self._deny("只能操作沙盒内的文件")
        if not full.exists():
            full = self._match_spaces(full)  # 只在沙盒内找，同一文件夹里的结果也必然在沙盒内
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

    def rel(self, full: Path) -> str:
        r = full.relative_to(self.root).as_posix()
        return r if r != "." else "."

    def _deny(self, why: str):
        self.violations += 1
        raise SandboxError(f"拒绝：{why}")


def is_hidden(path: Path) -> bool:
    """.DS_Store、.trash、.oplog 等点开头的文件一律对模型隐藏。"""
    return path.name.startswith(".")
