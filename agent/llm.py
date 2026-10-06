"""封装模型调用：重试、Token 与成本统计。"""
import os
import time

import openai
from openai import OpenAI

RETRYABLE = (openai.APITimeoutError, openai.APIConnectionError,
             openai.RateLimitError, openai.InternalServerError)


class LLM:
    def __init__(self, cfg: dict, client=None):
        self.model = cfg["model"]
        self.temperature = cfg.get("temperature", 0)
        if client is None:
            key = os.environ.get(cfg["api_key_env"])
            if not key:
                raise SystemExit(f"环境变量 {cfg['api_key_env']} 未设置，请在 ~/.zshrc 里 export 后重开终端")
            client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=60)
        self.client = client
        self.prices = (cfg.get("prices") or {}).get(self.model)  # 元/百万 tokens：cache_hit / cache_miss / output
        self.prompt_tokens = 0
        self.cache_hit_tokens = 0
        self.completion_tokens = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def cost(self) -> float:
        """累计花费（元）。没配置该模型价格时返回 0。"""
        if not self.prices:
            return 0.0
        miss = self.prompt_tokens - self.cache_hit_tokens
        return (self.cache_hit_tokens * self.prices["cache_hit"] + miss * self.prices["cache_miss"]
                + self.completion_tokens * self.prices["output"]) / 1e6

    def chat(self, messages: list[dict], tools: list[dict] | None = None, retries: int = 3):
        """返回 assistant 消息对象（有 .content / .tool_calls）。网络类错误指数退避重试。"""
        kwargs = dict(model=self.model, messages=messages, temperature=self.temperature)
        if tools:
            kwargs["tools"] = tools
        for attempt in range(retries + 1):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                break
            except RETRYABLE:
                if attempt == retries:
                    raise
                time.sleep(2 ** attempt)
        if resp.usage:
            u = resp.usage
            self.prompt_tokens += u.prompt_tokens
            self.completion_tokens += u.completion_tokens
            # DeepSeek 用 prompt_cache_hit_tokens；OpenAI 格式是 prompt_tokens_details.cached_tokens
            hit = getattr(u, "prompt_cache_hit_tokens", None)
            if hit is None:
                hit = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)
            self.cache_hit_tokens += hit or 0
        return resp.choices[0].message
