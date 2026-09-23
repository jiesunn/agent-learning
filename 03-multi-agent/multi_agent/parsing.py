# coding: utf-8
"""从模型回复里抠出 JSON。

为什么需要这个模块
------------------
02 章踩过的坑：GLM 在空参数时会返回 ``""`` / ``"{}"`` / ``"null"`` 三种形态。
到了 03 章，Planner 和 Evaluator 的**全部输出**都是 JSON，这个问题的暴露面
从"工具参数"扩大到了"控制流输入" —— 解析失败不再是少调一次工具，而是整个流程走不下去。

实测下来模型会给出这些形态（都不是合法 JSON 本身）：

1. ```json\\n{...}\\n```          —— 套了 markdown 围栏
2. 好的，我的计划如下：\\n{...}    —— 前面有一句人话
3. {...}\\n希望对你有帮助          —— 后面有一句人话
4. 正常 JSON

所以策略是三级降级：直接解析 -> 去围栏 -> 花括号配对扫描。
**不做**的：容忍尾逗号、单引号、注释。这些是"帮模型改错"，会把格式问题藏到更难查的地方。
"""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def strip_fences(text: str) -> str:
    """去掉 markdown 代码围栏，返回围栏内的内容；没有围栏则原样返回。"""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _scan_braces(text: str) -> dict | None:
    """花括号配对扫描，返回第一个能解析成 dict 的片段。

    必须感知字符串与转义：``{"note": "a } b"}`` 里的 ``}`` 不是闭合符。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False

        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue

            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # 这段不合法，从下一个 { 重新开始
                    if isinstance(parsed, dict):
                        return parsed
                    break

        start = text.find("{", start + 1)

    return None


def extract_json(text: str | None) -> dict | None:
    """尽最大努力把模型回复解析成 dict。失败返回 None，由调用方决定怎么降级。"""
    if not text:
        return None

    raw = text.strip()
    if not raw:
        return None

    # 一级：直接解析
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 二级：去围栏
    unfenced = strip_fences(raw)
    if unfenced != raw:
        try:
            parsed = json.loads(unfenced)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 三级：花括号配对扫描（覆盖"去围栏后仍混着人话"的情况）
    return _scan_braces(unfenced)
