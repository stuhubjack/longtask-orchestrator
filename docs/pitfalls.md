# 踩坑记录

> 这些都是实际跑长任务时撞出来的。每条包含：现象 → 根因 → 处置。
> 坑的价值在于，它们大多**不会在短任务里暴露**。

---

## 一、消息配对：长任务的 400 之痛

**现象**：任务跑到第 20+ 轮，模型返回 HTTP 400，此后每轮都 400，任务静默死亡。

**根因**：上下文裁剪时拆散了 `assistant(tool_calls)` 与对应 `tool` 响应的配对。
OpenAI 风格的消息协议要求带 tool_calls 的 assistant 消息后面必须跟足量 tool 响应；
裁剪后如果开头的 tool 响应变成"孤儿"（它的 assistant 已被切走），协议校验直接失败。

**错误做法**：
```python
# 天真裁剪：直接取最后 N 条
messages = messages[-50:]
# 若 tail[0] 恰好是 tool 响应 → 孤儿 → 400
```

**正确做法**：
```python
tail = messages[-50:]
while tail and tail[0].get("role") == "tool":
    tail.pop(0)          # 丢掉开头的孤儿 tool
messages = messages[:1] + [compress_marker] + tail
```

**还有个更隐蔽的变体**：压缩标记**覆盖**了 `tail[0]`。
若 `tail[0]` 是 `assistant(tool_calls)`，覆盖它会让**它后面的所有 tool 响应**变孤儿。
所以压缩消息只能**插入**，不能覆盖。

---

## 二、工具输出是上下文杀手

**现象**：跑到中段突然变"傻"，忘记前面已经确认过的事实，开始重复劳动。

**根因**：某条命令吐了几 MB（比如 `find /` 或一个大 JSON），
单次就把上下文预算吃光，触发了激进裁剪，早期关键结论被清掉。

**处置**：
- **在工具层截断**，不要指望模型自己要求裁剪
- 单条输出上限 6k 字符是经验值（能容下大部分结构化结果）
- 需要完整输出时，**落盘 + 回报路径**，让模型按需读取

```python
def run_command(cmd, timeout=60):
    out = subprocess.run(...).stdout
    return out[:6000] or "(no output)"   # 截断是必需，不是优化
```

---

## 三、reasoning_content 丢失

**现象**：换用带思维链的模型后，决策质量明显下降，开始做无意义的重复动作。

**根因**：回填 assistant 消息时只保留了 `content`，
把 `reasoning_content`（推理过程）丢了。部分模型依赖该字段维持推理连续性。

**处置**：回填时原样带上。

```python
messages.append({
    "role": "assistant",
    "content": content or "",
    "reasoning_content": msg.get("reasoning_content") or "",   # 别丢
    "tool_calls": tool_calls,
})
```

> 该字段名依提供商而异（`reasoning_content` / `reasoning` / 内联在 content 里）。
> 接入新提供商时先打一次原始响应看结构。

---

## 四、把环境故障当成任务完成

**现象**：任务"成功结束"，但产出为空。人以为任务做完了，实际是网络断了。

**根因**：`LLM_FAIL` 被当成正常终止。

**处置**：错误必须分类，处置完全不同：

| 错误特征 | 真实含义 | 动作 |
|---------|---------|------|
| `Temporary failure in name resolution` | DNS 环境问题 | 修复后续跑，**勿换目标** |
| 连接超时 / 503 | 网关故障 | 等一会重试 |
| HTTP 400 | 消息结构问题 | 见坑一 |
| HTTP 401/402 | 鉴权/额度 | 停止，人工处理 |
| HTTP 429 | 限流 | 退避重试 |

**判据**：任务是否真的穷尽，要看**产出内容**，不是看进程退出码。

---

## 五、空转：模型不给动作

**现象**：模型回复了一段"我将要……"的描述，但没有 tool_calls，循环空转直到耗尽轮次。

**根因**：模型的输出没有触发工具调用，而循环只处理 tool_calls 分支，直接 continue 回到下一轮。

**处置**：检测到无动作时，**推一把**而不是静默重试：

```python
if not tool_calls:
    messages.append({"role": "assistant", "content": content or "(no action)"})
    messages.append({"role": "user",
        "content": "请继续执行下一个具体命令，或在完成/放弃时明确输出 DONE 或 STOP。"})
    continue
```

**配套**：`DONE`/`STOP` 必须写进 system prompt 的终止约定里，
否则模型不知道可以停，会一直找事情做。

---

## 六、单轮多动作导致不可回放

**现象**：日志对不上——某轮出错了，但说不清是哪一步产生的。

**根因**：一轮里执行了多个动作，日志混在一起。

**处置**：**约定每轮一个动作**，并在 prompt 里明确写出来。
代价是轮次变多，收益是每一步都可定位、可复现、可中断续跑。

这与"上下文预算"是一对取舍：单动作省上下文，但费轮次。
长任务场景下**可观测性优先级更高**。

---

## 七、子任务上下文污染

**现象**：派出去的子任务"想太多"，把上个子任务的假设当成本次的前提。

**根因**：复用了同一个上下文，或交接时传了全文而非结论。

**处置**：
- 子任务**一律用独立上下文**执行
- 交接只传结构化摘要 + 产出路径（见架构文档的交接契约）
- 需要细节时，让子任务**自己去读文件**，而不是把文件内容塞进 prompt

---

## 八、终止条件形同虚设

**现象**：任务说"完成 10 个目标就停"，结果做了 3 个就报完成。

**根因**：终止条件写在任务书里，但**没写进 system prompt 的运行约束**，
模型读到的是"尽力而为"，不是"达成 N 个才算完成"。

**处置**：终止条件必须同时出现在
1. 任务书的**目标**段落（说清要什么）
2. system prompt 的**运行约束**（说清什么算完）

两处措辞要一致，否则模型会按更宽松的那个执行。

---

## 九、假成功：工具报错，模型说完成了

**现象**：进程正常退出，输出 `DONE`，日志无异常——但产出文件是空的，
或者根本不存在。这是长任务里最难发现的一类失败。

**根因**：模型判断"完成"的依据是它的意图，不是外部世界的状态。
当某条命令实际失败了（路径错、权限不足、命令不存在），
模型看到工具返回了东西，就认为这一步过了，继续往下走直到宣称完成。

**实例**：一条写入命令因路径解析错误失败了，
但 `&& echo written` 里的 `echo` 仍执行并返回，
模型看到 "written" 就认为写成功了。

**处置**：**终止时必须校验产出**，不能信任模型的自述。

```python
def check_termination(content, workdir, findings_path):
    if "DONE" not in content:
        return True, None                 # 非终止消息，放行
    if os.path.exists(findings_path):
        return True, None                 # 产出存在，允许终止
    return False, (                       # 产出缺失，打回自查
        "你宣布了 DONE，但产出文件 %s 不存在。\n"
        "请检查前面的命令是否真的成功执行（不要只看命令是否返回，要看产出）。"
        % findings_path
    )
```

配套：回填给模型时明确告诉它"不要只看命令是否返回，要看产出"。
模型通常能自己发现并修正。

**推广**：任何"模型自述的结果"都值得用外部证据校验一次——
文件是否真存在、接口是否真返回、数据是否真变化。

---

## 十、跨平台路径解析不一致（Windows 特别版）

**现象**：在 Windows 上跑，工具命令返回成功，但文件出现在**奇怪的位置**——
比如 `project/D:/home/user/output/result.txt`，
即在相对目录下又凭空创建了一整条绝对路径。

**根因**：Windows 上的 bash 有多个来源（MSYS/git-bash、WSL、Cygwin），
它们对绝对路径的解析规则**互不兼容**：

| 写法 | MSYS/git-bash | WSL | 说明 |
|------|--------------|-----|------|
| `D:/dir/file` | ⚠️ 被当作相对路径 | ✅ | `D:` 后跟 `/` 不是盘符语法 |
| `D:\dir\file` | ✅ | ⚠️ | 需转义，且在字符串里容易出错 |
| `/d/dir/file` | ✅ | ❌ | WSL 用 `/mnt/d/` |
| `/mnt/d/dir/file` | ❌ | ✅ | MSYS 不认 |

更麻烦的是：**同一个脚本里，`bash -c` 和 `subprocess` 调 bash 的行为可能不同**
（前者经过 shell 的路径转换，后者不经过）。

**处置**：
1. **工具命令一律用相对路径**，把工作目录交给 `cwd` 参数控制：

```python
def run_command(cmd, timeout=60, cwd=None):
    return subprocess.run(["bash", "-lc", cmd], ..., cwd=cwd)
```

2. 需要绝对路径时，从环境变量读，不要在命令里硬编码：

```python
# 好：由程序决定工作目录，命令里用相对路径
run_command("echo result > findings.md", cwd=workdir)

# 坏：路径拼进命令字符串，跨平台必炸
run_command("echo result > %s/findings.md" % workdir)
```

3. 验证方式：**写完立刻 `ls` 确认**，不要只看命令返回码。
