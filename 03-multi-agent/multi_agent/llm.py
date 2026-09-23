# coding: utf-8
"""LLM 客户端封装（从 02 拷贝 + 两处改动）。

改动 1：显式重试
    02 章把"失败注入未做"写进了"未做的事"。03 章一次运行要发 10-30 次请求，
    任何一次网络抖动都会让整条状态机挂在中间 —— 重试从"nice to have"变成必需。

    注意 ``max_retries=0``：openai SDK 自带 2 次重试，如果不清零，
    我们的重试逻辑和 SDK 的重试会叠乘（最坏 9 次请求），
    实测耗时和 token 统计都会失真。**两套重试机制不能同时开着。**

改动 2：temperature 可显式指定
    Planner / Evaluator 的输出要进 JSON 解析，用 0.0 减少格式抖动；
    Executor 保持服务端默认，避免把 01/02 的行为也改掉。
"""
from __future__ import annotations

import os
import time
from collections.abc import Callable

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEFAULT_MAX_RETRIES = 2
DEFAULT_TIMEOUT = 120.0


class LLMClient:
    """LLM 客户端封装类。"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = None

    @property
    def client(self):
        if self._client is None:
            # max_retries=0：重试由 call() 自己管，不让 SDK 再重试一遍
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=0,
                timeout=DEFAULT_TIMEOUT,
            )
        return self._client

    def call(
        self,
        messages,
        tools=None,
        temperature: float | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        on_retry: Callable[[int, Exception], None] | None = None,
    ):
        """封装一次 LLM 调用，带指数退避重试。

        tools 为空时不传该参数，避免部分服务在 tools=[] 时报错。
        """
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if temperature is not None:
            kwargs["temperature"] = temperature

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return self.client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001 - 网络/协议错误都要重试
                last_error = e
                if attempt >= max_retries:
                    break
                if on_retry is not None:
                    on_retry(attempt + 1, e)
                time.sleep(2 ** attempt)

        raise RuntimeError(
            f"LLM call failed after {max_retries + 1} attempts: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error


_LLM_CLIENTS = {
    "deepseek": LLMClient(
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("DEEPSEEK_BASE_URL", ""),
        model=os.getenv("DEEPSEEK_MODEL", ""),
    ),
    "glm": LLMClient(
        api_key=os.getenv("GLM_API_KEY", ""),
        base_url=os.getenv("GLM_BASE_URL", ""),
        model=os.getenv("GLM_MODEL", ""),
    ),
}


def get_current_client() -> LLMClient:
    """返回当前选中的 LLM client。"""
    llm_name = os.getenv("LLM_NAME", "deepseek")
    client = _LLM_CLIENTS.get(llm_name)
    if not client:
        raise ValueError(f"未找到 LLM 客户端: {llm_name}")
    return client
