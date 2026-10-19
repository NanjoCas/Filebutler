"""封装模型调用：重试、Token 与成本统计。"""
import os
import json
import time

import openai
from openai import OpenAI

RETRYABLE = (openai.APITimeoutError, openai.APIConnectionError,
             openai.RateLimitError, openai.InternalServerError)


class LLM:
    def __init__(self, cfg: dict, client=None):
        self.model = cfg["model"]
        self.temperature = cfg.get("temperature", 0)
        self.retries = max(0, min(3, int(cfg.get("llm_retries", 1))))
        self.timeout = max(1, float(cfg.get("llm_timeout", 60)))
        if client is None:
            key = os.environ.get(cfg["api_key_env"])
            if not key:
                raise SystemExit(f"环境变量 {cfg['api_key_env']} 未设置，请在 ~/.zshrc 里 export 后重开终端")
            client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=self.timeout, max_retries=0)
        self.client = client
        self.prices = (cfg.get("prices") or {}).get(self.model)  # 元/百万 tokens：cache_hit / cache_miss / output
        self.prompt_tokens = 0
        self.cache_hit_tokens = 0
        self.completion_tokens = 0
        self.last_prompt_tokens = None  # 上一次调用的准确输入长度，上下文压缩用它估算当前长度

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

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             retries: int | None = None, on_request=None):
        """One retry policy; diagnostics never contain prompts, keys or response bodies."""
        kwargs = dict(model=self.model, messages=messages, temperature=self.temperature)
        if tools:
            kwargs["tools"] = tools
        limit = self.retries if retries is None else max(0, min(3, retries))
        input_chars = len(json.dumps(messages, ensure_ascii=False))
        for attempt in range(limit + 1):
            started = time.perf_counter()
            if on_request:
                on_request(dict(phase="start", attempt=attempt + 1, input_chars=input_chars, messages=len(messages)))
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                retry = isinstance(exc, RETRYABLE) and attempt < limit
                delay = 2 ** attempt if retry else 0
                if on_request:
                    on_request(dict(phase="end", attempt=attempt + 1, ok=False,
                                    ms=round((time.perf_counter() - started) * 1000),
                                    error_type=type(exc).__name__, status=getattr(exc, "status_code", None),
                                    retry=retry, backoff_seconds=delay))
                if not retry:
                    raise
                time.sleep(delay)
            else:
                u = resp.usage
                hit = getattr(u, "prompt_cache_hit_tokens", None)
                if hit is None:
                    hit = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0)
                if on_request:
                    on_request(dict(phase="end", attempt=attempt + 1, ok=True,
                                    ms=round((time.perf_counter() - started) * 1000),
                                    prompt_tokens=getattr(u, "prompt_tokens", 0),
                                    completion_tokens=getattr(u, "completion_tokens", 0),
                                    cache_hit_tokens=hit or 0, retry=False))
                break
        if resp.usage:
            u = resp.usage
            self.prompt_tokens += u.prompt_tokens
            self.last_prompt_tokens = u.prompt_tokens
            self.completion_tokens += u.completion_tokens
            # DeepSeek 用 prompt_cache_hit_tokens；OpenAI 格式是 prompt_tokens_details.cached_tokens
            hit = getattr(u, "prompt_cache_hit_tokens", None)
            if hit is None:
                hit = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None)
            self.cache_hit_tokens += hit or 0
        return resp.choices[0].message
