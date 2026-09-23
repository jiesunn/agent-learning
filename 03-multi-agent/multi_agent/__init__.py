# coding: utf-8
"""03-multi-agent：Planner / Executor / Evaluator + 显式状态机。

模块划分（按"依赖方向"排，上层依赖下层，没有反向依赖）::

    state.py         数据模型 + 纯函数状态机（不依赖任何东西，可单测）
    parsing.py       模型输出 -> dict 的降级解析（不依赖任何东西，可单测）
    tools.py         工具注册表 + 权限裁剪
    llm.py           LLM 客户端（重试、超时、max_retries=0）
    logger.py        按角色归因 token + 状态转移事件流
    agents.py        三个角色（依赖上面全部）
    orchestrator.py  装配 + 主循环（依赖 agents）
    runner.py        多 Agent / 单 Agent 两个入口（依赖 orchestrator）
    main.py          CLI
"""

__all__ = ["state", "parsing", "tools", "llm", "logger", "agents", "orchestrator", "runner"]
