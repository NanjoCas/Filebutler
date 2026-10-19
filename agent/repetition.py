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
