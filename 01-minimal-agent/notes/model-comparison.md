# 模型对比：DeepSeek vs GLM

## 测试任务
创建 hello.txt，若已存在则返回内容。

## 结果
两个模型都跑通，都是 3 轮循环、2 次工具调用、最后 stop。

## 差异

| 维度 | DeepSeek | GLM |
|------|----------|-----|
| tool_call_id 格式 | `call_00_xxx` | `call_-724398...`（带负数） |
| tool_calls 时 content | None / '' | 有实际文字（自言自语） |
| content 换行 | 干净 | 前后有 \n |

## 结论
1. 不要硬编码解析 tool_call_id
2. content 的 None / '' / 有内容三种情况都要能处理
3. 展示给用户前应 strip()
4. OpenAI 兼容协议下，切换模型只需改 base_url + model

## 对代码的影响
- `llm.py`：抽象 client，隔离差异 ✅
- `tools.py`：无感知 ✅
- `main.py`：只判断 finish_reason，不关心 content 值 ✅