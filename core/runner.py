#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Long-Task Runner —— 长任务执行器

在受限上下文里跑长任务的最小实现：LLM 决策 -> 工具执行 -> 结果回填 -> 直到终止条件。
解决的核心问题是「长任务中的记忆衰减」：把不稳定的长上下文，换成稳定的
「循环 + 外部状态 + 结构化裁剪」。

运行:
    python3 runner.py <task_file> [workdir] [model] [max_turns] [seed_file]

  task_file  任务委托书（纯文本/Markdown），作为 system prompt 的一部分
  workdir    产出目录（findings.md / agent.log 落盘位置），默认 ./work
  model      模型名，默认读环境变量 LONGTASK_MODEL
  max_turns  最大决策轮次，默认 60
  seed_file  可选：首轮 user 消息。续跑时把「已完成产物路径 + 剩余线索」写进去，
             让新会话从干净上下文起步，避免超长历史拖垮请求

设计要点（都是踩过坑换来的）:
  1. 上下文裁剪必须保持 assistant(tool_calls) ↔ tool 响应配对，否则网关直接 400
  2. 裁剪时压缩提示要「插入」而不是「覆盖」，覆盖会让配对的 tool 变孤儿
  3. 部分模型需要回填 reasoning_content，否则推理链丢失、决策质量下降
  4. LLM 调用失败要区分「环境故障」和「任务穷尽」——前者重试，后者收尾
  5. 单轮只执行一个动作，便于日志可回放、失败可定位
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------- 配置
API = os.environ.get("LONGTASK_API", "https://api.openai.com/v1/chat/completions")
MODEL = os.environ.get("LONGTASK_MODEL", "gpt-4o-mini")
API_KEY = os.environ.get("LONGTASK_API_KEY", "")
DEFAULT_TASK = "./task.md"
DEFAULT_WORKDIR = "./work"
FINDINGS = "findings.md"
LOG = "agent.log"
MAX_TURNS = 60
CMD_TIMEOUT = 60
REQUEST_TIMEOUT = 180

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

LAST_LLM_ERR = None  # 最近一次 LLM 调用异常详情，失败时落盘


def log(msg, workdir):
    """每轮决策与工具结果落盘，保证任务可审计、可回放。"""
    path = os.path.join(workdir, LOG)
    with open(path, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().isoformat(), msg))


# ---------------------------------------------------------------- LLM
def call_llm(messages, tools, key, max_tokens=8000, workdir=""):
    """调用模型。返回 None 表示不可恢复的失败（调用方应终止并落盘）。"""
    global LAST_LLM_ERR
    body = {
        "model": MODEL,
        "messages": messages,
        "tools": tools,
        "max_tokens": max_tokens,
        "stream": False,
    }
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - 需要捕获全部网络异常
            LAST_LLM_ERR = str(e)
            detail = ""
            if hasattr(e, "read"):
                try:
                    detail = e.read().decode("utf-8", errors="replace")[:400]
                except Exception:  # noqa: BLE001
                    pass
            log("llm error attempt %d: %s %s" % (attempt, e, detail), workdir)
            dump_failed_request(body, workdir)

            # 4xx 多为请求结构问题，重试无益，直接失败
            if hasattr(e, "code") and e.code in (400, 401, 402, 413, 429):
                return None
            time.sleep(10)
    return None


def dump_failed_request(body, workdir):
    """把失败请求的结构信息落盘，便于事后定位（不落完整内容，防泄露）。"""
    if not workdir:
        return
    try:
        payload = {
            "ts": datetime.now().isoformat(),
            "n_msgs": len(body.get("messages", [])),
            "total_chars": len(json.dumps(body, ensure_ascii=False)),
            "roles": [
                {
                    "r": m.get("role"),
                    "tc": bool(m.get("tool_calls")),
                    "tcid": m.get("tool_call_id", ""),
                }
                for m in body.get("messages", [])
            ][:200],
        }
        with open(
            os.path.join(workdir, "llm_req_dump.jsonl"), "a", encoding="utf-8"
        ) as df:
            df.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 工具
def run_command(cmd_str, timeout=CMD_TIMEOUT, cwd=None):
    """执行一条 shell 命令并返回截断后的输出。

    cwd 传工作目录：让工具在产出目录里执行相对路径命令。
    这很重要——Windows 上不同 bash 实现（MSYS/WSL/Cygwin）对绝对路径的
    解析规则不一致（D:/x、/d/x、D:\\x 各有差异），相对路径是唯一稳的写法。

    输出必须截断：单条命令吐几 MB 会瞬间吃掉上下文预算，长任务就此失忆。
    """
    try:
        p = subprocess.run(
            ["bash", "-lc", cmd_str],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
        )
        out = (p.stdout or "") + ("\n[stderr] " + p.stderr if p.stderr else "")
        return out[:6000] or "(no output)"
    except subprocess.TimeoutExpired:
        return "(timeout after %ss)" % timeout
    except Exception as e:  # noqa: BLE001
        return "(error: %s)" % e


def build_tools():
    """工具集定义。此处只给一个 shell 工具，够用且边界清晰。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": (
                    "在 Linux/macOS 上执行一条 shell 命令。"
                    "每次只执行一个动作，保持可观测与可回放。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "cmd": {"type": "string", "description": "要执行的 shell 命令"}
                    },
                    "required": ["cmd"],
                },
            },
        }
    ]


# ---------------------------------------------------------------- 上下文管理
def trim_messages(messages, limit=60, keep=50):
    """上下文裁剪 —— 长任务的核心机制。

    目标：把消息数压回预算内，同时不破坏协议约束。

    两个必须遵守的约束（违反即网关 400）：
      A. assistant(tool_calls) 与对应 tool 响应必须成对出现
      B. 压缩提示只能「插入」到尾部之前，绝不能覆盖 tail[0]

    做法：保留 system 消息 + 一条压缩标记 + 最近的 N 条，
         并确保裁剪后的第一条不是孤儿 tool 响应。
    """
    if len(messages) <= limit:
        return messages

    tail = messages[-keep:]
    # 约束 A：丢掉开头孤立的 tool 响应（其 assistant 已被切走）
    while tail and tail[0].get("role") == "tool":
        tail.pop(0)

    # 约束 B：压缩标记插入而非覆盖
    new_messages = (
        messages[:1]
        + [{"role": "user", "content": "(早期对话已压缩) 继续当前任务。"}]
        + tail
    )

    # 若结尾是带 tool_calls 的 assistant 而工具结果已被丢弃，给它一个收束消息
    last = new_messages[-1]
    if last.get("role") == "assistant" and last.get("tool_calls"):
        new_messages.append(
            {"role": "user", "content": "(后续工具调用结果已丢弃) 请继续当前任务。"}
        )
    return new_messages


def check_termination(content, workdir, findings_path):
    """终止校验 —— 防止「假成功」。

    模型宣布 DONE 时，必须确认产出文件真的存在。
    否则会出现「工具报错但模型说完成了」的静默失败——
    这在长任务里极难发现，因为进程正常退出、日志无异常。

    返回 (可终止, 提示消息)
    """
    if "DONE" not in content:
        return True, None

    if os.path.exists(findings_path):
        return True, None

    return False, (
        "你宣布了 DONE，但产出文件 %s 不存在。\n"
        "请检查前面的命令是否真的成功执行（不要只看命令是否返回，要看产出）。\n"
        "确认产出写入后再回复 DONE；若确实无法写入，回复 STOP 并说明原因。"
        % findings_path
    )


# ---------------------------------------------------------------- 主循环
def main():
    task_file = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK
    workdir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_WORKDIR

    global MODEL, MAX_TURNS
    if len(sys.argv) > 3:
        MODEL = sys.argv[3]
    if len(sys.argv) > 4:
        MAX_TURNS = int(sys.argv[4])
    seed_file = sys.argv[5] if len(sys.argv) > 5 else None

    os.makedirs(workdir, exist_ok=True)

    if not API_KEY:
        print("ERROR: LONGTASK_API_KEY 未设置")
        return 2

    task = open(task_file, encoding="utf-8").read()
    findings_path = os.path.join(workdir, FINDINGS)

    system = task + (
        "\n\n## 运行约束\n"
        "- 你通过 bash 工具执行所有操作，每步思考后执行一个命令\n"
        "- 改动外部状态前先确认范围；不确定就停下来说明\n"
        f"- 任务完成：把结果写入 {findings_path} 并回复 DONE\n"
        f"- 确认无法推进：把阶段性结论写入 {findings_path} 并回复 STOP\n"
        "- 回复时先用简短中文说明当前进展，再决定下一步"
    )

    if seed_file:
        seed = open(seed_file, encoding="utf-8").read()
    else:
        seed = "开始：按任务书执行第一步。每轮只执行一个命令，保持节奏与可观测性。"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": seed},
    ]

    log("agent start | model=%s max_turns=%d" % (MODEL, MAX_TURNS), workdir)
    tools = build_tools()

    for turn in range(MAX_TURNS):
        log("turn %d call llm" % turn, workdir)
        resp = call_llm(messages, tools, API_KEY, workdir=workdir)
        if not resp:
            log("llm failed, abort: %s" % LAST_LLM_ERR, workdir)
            print("LLM_FAIL")
            return 1

        msg = resp["choices"][0]["message"]
        content = msg.get("content") or ""

        if content:
            log("assistant: %s" % content[:300], workdir)
            print("[assistant] %s" % content[:500])

            if "STOP" in content:
                print("AGENT_FINISHED")
                return 0

            if "DONE" in content:
                ok, hint = check_termination(content, workdir, findings_path)
                if ok:
                    print("AGENT_FINISHED")
                    return 0
                # 假成功：产出缺失，打回让它自查
                log("termination rejected: findings missing", workdir)
                print("[guard] DONE 被拒绝：产出文件不存在")
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": hint})
                messages = trim_messages(messages)
                continue

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            # 模型没给动作：推它一把，避免空转
            messages.append({"role": "assistant", "content": content or "(no action)"})
            messages.append(
                {
                    "role": "user",
                    "content": "请继续执行下一个具体命令，或在完成/放弃时明确输出 DONE 或 STOP。",
                }
            )
            continue

        messages.append(
            {
                "role": "assistant",
                "content": content or "",
                # 部分模型依赖该字段保持推理连续性
                "reasoning_content": msg.get("reasoning_content") or "",
                "tool_calls": tool_calls,
            }
        )

        for tc in tool_calls:
            fn = tc["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:  # noqa: BLE001
                args = {"cmd": fn.get("arguments", "")}

            cmd = args.get("cmd", "")
            log("tool command: %s" % cmd[:200], workdir)
            result = run_command(cmd, cwd=workdir)
            log("tool result: %s" % result[:200], workdir)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result[:2000],
                }
            )

        messages = trim_messages(messages)

    log("max turns reached", workdir)
    print("MAX_TURNS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
