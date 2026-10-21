"""连续重复调用计数；只规范化 JSON 写法，不改动路径或补默认参数。"""
import json


class RepeatDetector:
    def __init__(self):
        self.last = None
        self.count = 0

    def observe(self, name: str, raw: str) -> int:
        try:
            args = json.dumps(json.loads(raw or "{}"), sort_keys=True, ensure_ascii=False)
        except (ValueError, TypeError):
            args = raw  # 非法 JSON 也可能原地重试，按原文比较
        key = (name, args)
        self.count = self.count + 1 if key == self.last else 1
        self.last = key
        return self.count


class NoProgressGuard:
    """Bound failures by machine error class, independent of argument spelling.

    Successful contract edits are not execution progress. Only a changed local
    execution/evidence fingerprint resets the repair budget.
    """
    def __init__(self, limit=6):
        self.limit = max(0, int(limit))
        self.progress = None
        self.failures = {}

    def observe(self, failure_group, progress):
        if progress != self.progress:
            self.progress, self.failures = progress, {}
        if not failure_group or not self.limit:
            return False
        self.failures[failure_group] = self.failures.get(failure_group, 0) + 1
        return self.failures[failure_group] >= self.limit
