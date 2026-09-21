# coding: utf-8
"""Agent 运行日志 + token 统计。"""
import json
import time
from datetime import datetime
from pathlib import Path

LOG_DIR = Path("./logs")


class RunLogger:
    """记录一次 Agent 运行的完整过程，结束时落盘为 JSON。"""

    def __init__(self, user_input: str, model: str = "unknown"):
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.user_input = user_input
        self.model = model
        self.start_time = time.time()
        self.rounds: list[dict] = []
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_tokens = 0
        LOG_DIR.mkdir(exist_ok=True)

    def log_round(self, round_num, messages_before, response, tool_results, duration):
        """记录一轮的输入、输出、工具结果、耗时、token。"""
        choice = response.choices[0]
        msg = choice.message

        self.rounds.append({
            "round": round_num,
            "duration_ms": round(duration * 1000),
            "messages_before": messages_before,
            "response": {
                "finish_reason": choice.finish_reason,
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (msg.tool_calls or [])
                ],
            },
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            },
            "tool_results": tool_results,
        })

        self.total_prompt_tokens += response.usage.prompt_tokens
        self.total_completion_tokens += response.usage.completion_tokens
        self.total_tokens += response.usage.total_tokens

    def finish(self, final_answer, status: str):
        """结束运行，落盘。"""
        end_time = time.time()
        data = {
            "run_id": self.run_id,
            "model": self.model,
            "user_input": self.user_input,
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "end_time": datetime.fromtimestamp(end_time).isoformat(),
            "duration_seconds": round(end_time - self.start_time, 2),
            "status": status,
            "final_answer": final_answer,
            "rounds": self.rounds,
            "summary": {
                "num_rounds": len(self.rounds),
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
            },
        }
        log_file = LOG_DIR / f"{self.run_id}.json"
        log_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return log_file

    def print_summary(self):
        """打印统计到控制台。"""
        print("\n" + "=" * 50)
        print("📊 本次运行统计")
        print("=" * 50)
        print(f"run_id:              {self.run_id}")
        print(f"轮数:                 {len(self.rounds)}")
        print(f"Prompt tokens:        {self.total_prompt_tokens}")
        print(f"Completion tokens:    {self.total_completion_tokens}")
        print(f"Total tokens:         {self.total_tokens}")
        total_ms = sum(r["duration_ms"] for r in self.rounds)
        print(f"LLM 总耗时:           {total_ms} ms")
        print("=" * 50)
