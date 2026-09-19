---
name: context-management
description: 长任务的上下文预算与状态外置策略：预算分配、裁剪规则、续跑恢复；Use when 任务轮次可能超过 20 轮或单步产出较大时；Do NOT use for 短任务（开销大于收益）
version: 1.0.0
tags: [context, memory, long-task]
---

# 上下文管理

## 问题定义

所有 LLM 长任务最终都会撞上同一堵墙：**上下文是有界的，而任务不是。**

天真做法是"把一切都留在对话里"，结果必然是：
对话膨胀 → 被迫裁剪 → 早期信息丢失 → 任务重复劳动/前后矛盾 → 失败。

**关键认知**：上下文不是记忆，是**工作台**。
工作台要干净，记忆应该放在抽屉里（磁盘）。

## 预算分配

以 128k 上下文为例的经验分配：

| 用途 | 占比 | 约合 | 说明 |
|------|------|------|------|
| system + 任务书 | 10% | 13k | 固定开销，尽量精简 |
| 近期对话 | 40% | 51k | 保留最近 15-25 轮完整交互 |
| 工具输出 | 30% | 38k | **单条截断到 6k 以内** |
| 安全余量 | 20% | 26k | 突发输出、重试、格式开销 |

**超预算时的处置顺序**（重要：按此顺序，不要跳步）：

1. **先截断工具输出**——信息密度最低，截断损失最小
2. **再压缩历史轮次**——保留结论与指针，丢弃过程细节
3. **最后才动任务书**——最高优先级，尽量不碰

顺序反了会出事：先压缩任务书会导致模型忘记目标本身，
这时就算上下文够用，任务也已经跑偏了。

## 裁剪规则

**必须遵守：保持消息协议配对。**

`assistant(tool_calls)` + 后续的 `tool` 响应是一条不可分割的链。
裁剪时：

```python
def trim(messages, limit=60, keep=50):
    if len(messages) <= limit:
        return messages

    tail = messages[-keep:]

    # 规则 1：丢弃开头的孤儿 tool 响应
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)

    # 规则 2：压缩提示「插入」，绝不「覆盖」tail[0]
    out = (messages[:1]
           + [{"role": "user", "content": "(早期对话已压缩) 继续当前任务。"}]
           + tail)

    # 规则 3：结尾若是待响应的 assistant(tool_calls)，补一条收束消息
    if out[-1].get("role") == "assistant" and out[-1].get("tool_calls"):
        out.append({"role": "user",
                    "content": "(后续工具结果已丢弃) 请继续当前任务。"})

    return out
```

规则 2 容易被写错成 `tail[0] = compress_marker`。
这在 `tail[0]` 是 `assistant(tool_calls)` 时会**让它后面的所有 tool 响应变孤儿**，
下一轮直接 400。

## 状态外置

**判据：如果一个结论只存在于对话里，它就已经丢失了。**

产出目录约定：

```
work/
├── findings.md         最终产出（人可读）
├── state.json          结构化状态（机可读）——续跑的关键
├── agent.log           逐轮决策日志（自动写入）
└── artifacts/          中间产物，按子任务分目录
```

`state.json` 建议结构：

```json
{
  "task_id": "...",
  "updated_at": "2026-01-01T12:00:00",
  "progress": {
    "completed": ["s01", "s02"],
    "current": "s03",
    "pending": ["s04", "s05"]
  },
  "facts": [
    {"key": "target/entry", "summary": "...", "level": "L3",
     "artifact": "artifacts/s01/result.json"}
  ],
  "constraints": ["已知限制1", "踩过的坑"],
  "next_hint": "下一步应该做什么"
}
```

**每完成一个子任务就更新一次**——不要攒到最后写
（攒到最后往往就没机会写了）。

## 结构化交接

子任务之间传递**结论 + 路径**，不传全文。

```json
{
  "subtask_id": "s03",
  "status": "done",
  "summary": "一句话结论",
  "artifacts": ["artifacts/s03/result.json"],
  "next_hint": "给下一个子任务的线索",
  "evidence_level": "L3"
}
```

下游需要细节时**自己去读文件**，而不是让上游把文件塞进 prompt。
这是"用空间换记忆"的具体落点。

## 续跑恢复

长任务几乎不可能一次跑完，**续跑能力是必需品**。

**错误做法**：把上一轮的完整对话接着往下传。
结果是上一轮的上下文膨胀原样带入，很快再次触顶——治标不治本。

**正确做法**：新开干净上下文，用 seed 文件恢复现场。

```markdown
# 续跑说明

## 已完成
- s01-s03 完成，产出在 artifacts/s01..s03/
- 关键结论：<一句话>

## 当前进度
- s03 进行到 <步骤>，中间产物 artifacts/s03/partial.json

## 下一步
- 继续 s03 的 <具体动作>

## 已知约束
- <踩过的坑，避免重复>
- <环境限制，如网络/权限问题>
```

这个 seed 文件通常几千字符，相比几十万字符的历史对话，
让模型把全部注意力放在"当前要做什么"上，效率天差地别。

## 工具输出的处理

```python
# 截断是必需，不是优化
def run_command(cmd, timeout=60):
    out = subprocess.run(["bash", "-lc", cmd], capture_output=True,
                         text=True, timeout=timeout)
    text = out.stdout + (out.stderr or "")
    return text[:6000] or "(no output)"
```

单条输出上限 6k 字符是经验值。需要完整输出时：

1. **落盘**：`cmd > work/artifacts/<id>/raw.txt`
2. **回报路径与摘要**：告诉模型"输出在 X，前几行是 Y"
3. 模型需要时按需用 `head`/`grep` 读取片段

这样既保住了完整数据，又只消耗少量上下文。

## 自查清单

任务跑到一半时，逐项检查：

- [ ] `state.json` 是否是最新的？（超过 5 轮没更新 = 危险）
- [ ] 最近一轮的工具输出有没有超长项？
- [ ] 对话轮次是否接近裁剪阈值？
- [ ] 任务目标是否还清晰地存在于 system prompt 里？
- [ ] 有没有结论只存在于对话中而没落盘？
