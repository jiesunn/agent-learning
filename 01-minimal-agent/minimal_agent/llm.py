# coding: utf-8
"""LLM 客户端封装。"""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

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
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def call(self, messages, tools=None):
        """封装一次 LLM 调用。

        tools 为空时不传该参数，避免部分服务在 tools=[] 时报错。
        """
        kwargs = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        return self.client.chat.completions.create(**kwargs)


_LLM_CLIENTS = {
    "deepseek": LLMClient(
        api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        base_url=os.getenv("DEEPSEEK_BASE_URL", ""),
        model=os.getenv("DEEPSEEK_MODEL", "")
    ),
    "glm": LLMClient(
        api_key=os.getenv("GLM_API_KEY", ""),
        base_url=os.getenv("GLM_BASE_URL", ""),
        model=os.getenv("GLM_MODEL", "")
    ),
}


def get_current_client() -> LLMClient:
    """返回当前选中的 LLM client。"""
    llm_name = os.getenv("LLM_NAME", "deepseek")
    client = _LLM_CLIENTS.get(llm_name)
    if not client:
        raise ValueError(f"未找到 LLM 客户端: {llm_name}")
    return client
