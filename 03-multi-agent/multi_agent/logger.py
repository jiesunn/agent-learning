# coding: utf-8
"""多 Agent 运行日志。

与 01/02 的 logger 差异
-----------------------
01/02 只有一个 Agent，token 只有一个总数，所以 ``RunLogger`` 按"轮"记就够了。

多 Agent 之后，**"总共花了多少 token"变成一个没有信息量的数字**。真正要回答的是：

    这 8500 个 token 里，多少花在规划上？多少花在执行上？多少花在评估上？
    评估器读文件复核，值不值这 1500 个 token？
    一次 replan 的代价，等于几次正常执行？

所以这一版 logger 做两件事：

1. **每次 LLM 调用都打上 agent 标签**（``log_llm_call``），结束时按角色聚合
2. **状态转移单独记事件流**（``log_event``），把"为什么走这一步"落盘

第 2 点尤其重要：多 Agent 出问题时，第一个要看的不是某次 LLM 回复，
而是**状态机的转移轨迹** —— 它比模型的输出小得多，也确定得多。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("./logs")


class RunLogger:
    """记录一次多 Agent 运行的完整过程，结束时落盘为 JSON。"""

    def __init__(self, goal: str, model: str = "unknown", mode: str = "multi"):
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.goal = goal
        self.model = model
        self.mode = mode
        self.start_time = time.time()

        self.calls: list[dict] = []
        self.events: list[dict] = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        self.retries = 0
        self.errors = 0

        LOG_DIR.mkdir(exist_ok=True)

    # ------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------
    def log_llm_call(self, *, agent: str, label: str, messages, response, duration: float):
        """记录一次 LLM 调用。agent 是 token 归因的唯一依据。"""
        choice = response.choices[0]
        msg = choice.message
        usage = response.usage

        record = {
            "seq": len(self.calls) + 1,
            "ts": round(time.time() - self.start_time, 3),
            "agent": agent,
            "label": label,
            "duration_ms": round(duration * 1000),
            "messages": messages,
            "response": {
                "finish_reason": choice.finish_reason,
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (msg.tool_calls or [])
                ],
            },
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
        self.calls.append(record)

        self.total_prompt_tokens += usage.prompt_tokens
        self.total_completion_tokens += usage.completion_tokens
        self.total_tokens += usage.total_tokens

    def log_event(self, event_type: str, **data):
        """记录一次状态机事件 / 工具调用 / 重试。"""
        if event_type == "llm_retry":
            self.retries += 1
        if event_type == "llm_error":
            self.errors += 1
        self.events.append({
            "ts": round(time.time() - self.start_time, 3),
            "type": event_type,
            **data,
        })

    # ------------------------------------------------------------
    # 聚合
    # ------------------------------------------------------------
    @property
    def llm_calls(self) -> int:
        """已发出的 LLM 调用次数。状态机的预算判断读这个值。"""
        return len(self.calls)

    def by_agent(self) -> dict:
        """按角色聚合 token 与耗时 —— 这是 03 章最核心的一张表。"""
        agg: dict[str, dict] = {}
        for c in self.calls:
            a = agg.setdefault(c["agent"], {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "duration_ms": 0,
            })
            a["calls"] += 1
            a["prompt_tokens"] += c["usage"]["prompt_tokens"]
            a["completion_tokens"] += c["usage"]["completion_tokens"]
            a["total_tokens"] += c["usage"]["total_tokens"]
            a["duration_ms"] += c["duration_ms"]
        return agg

    def summary(self) -> dict:
        return {
            "llm_calls": len(self.calls),
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_duration_ms": sum(c["duration_ms"] for c in self.calls),
            "retries": self.retries,
            "errors": self.errors,
            "by_agent": self.by_agent(),
        }

    # ------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------
    def finish(
        self,
        *,
        status: str,
        final_answer: str | None,
        state=None,
        extra: dict | None = None,
    ) -> Path:
        end_time = time.time()
        duration = end_time - self.start_time

        data = {
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "goal": self.goal,
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "end_time": datetime.fromtimestamp(end_time).isoformat(),
            "duration_seconds": round(duration, 2),
            "status": status,
            "final_answer": final_answer,
            "summary": self.summary(),
            "events": self.events,
            "calls": self.calls,
        }
        if state is not None:
            data["state"] = {
                "replans": state.replans,
                "abort_reason": state.abort_reason,
                "plan_revision": state.plan.revision if state.plan else None,
                "steps": [
                    {
                        "id": s.id,
                        "description": s.description,
                        "status": str(s.status),
                        "attempts": s.attempts,
                        "verdict": str(s.verdict) if s.verdict is not None else None,
                        "last_feedback": s.last_feedback,
                        "result": s.result,
                    }
                    for s in (state.plan.steps if state.plan else [])
                ],
            }
        if extra:
            data.update(extra)

        log_file = LOG_DIR / f"{self.run_id}_{self.mode}.json"
        log_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return log_file

    def print_summary(self):
        s = self.summary()
        print("\n" + "=" * 62)
        print("📊 本次运行统计")
        print("=" * 62)
        print(f"run_id:            {self.run_id}  (mode={self.mode})")
        print(f"LLM 调用次数:       {s['llm_calls']}  (重试 {s['retries']}, 失败 {s['errors']})")
        print(f"Total tokens:      {s['total_tokens']}")
        print(f"LLM 总耗时:         {s['llm_duration_ms']} ms")
        print("-" * 62)
        print(f"{'agent':<12}{'calls':>6}{'prompt':>10}{'completion':>12}{'total':>10}{'ms':>9}")
        for agent, a in s["by_agent"].items():
            print(
                f"{agent:<12}{a['calls']:>6}{a['prompt_tokens']:>10}"
                f"{a['completion_tokens']:>12}{a['total_tokens']:>10}{a['duration_ms']:>9}"
            )
        print("=" * 62)
