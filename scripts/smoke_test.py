#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端冒烟测试 —— 不需要真实 API

用一个本地 mock LLM 服务器验证 runner 的完整循环：
  LLM 决策 -> 工具执行 -> 结果回填 -> 上下文裁剪 -> 终止判定

用法:
    python3 scripts/smoke_test.py

退出码 0 = 通过
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
import time

PORT = 8931
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(ROOT, "core")

turn_counter = {"n": 0}
SEEN_REQUESTS = []
workdir_ref = [""]   # 由 main() 填充，MockLLM 用它生成绝对路径


class MockLLM(http.server.BaseHTTPRequestHandler):
    """按固定脚本驱动的假模型：执行两条命令后输出 DONE。"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        SEEN_REQUESTS.append(body)

        n = turn_counter["n"]
        turn_counter["n"] += 1

        if n == 0:
            msg = {
                "role": "assistant",
                "content": "第一步：确认环境。",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "bash",
                                 "arguments": json.dumps(
                                     {"cmd": "echo hello-from-tool && echo pwd=$(pwd)"})},
                }],
            }
        elif n == 1:
            msg = {
                "role": "assistant",
                "content": "第二步：写入产出。",
                "tool_calls": [{
                    "id": "call_2", "type": "function",
                    "function": {"name": "bash",
                                 # 用相对路径：Windows 上 subprocess 调 MSYS bash 时
                                 # 绝对路径（D:/... 或 /d/...）解析行为不一致，相对路径最稳
                                 "arguments": json.dumps(
                                     {"cmd": "echo '# smoke test result' > 'findings.md' && echo written"})},
                }],
            }
        else:
            # 只检查最后一条 user 消息：终止驳回的提示
            msgs = body.get("messages", [])
            last_user = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    last_user = str(m.get("content", ""))
                    break
            rejected = "产出文件" in last_user and "不存在" in last_user

            if rejected:
                msg = {
                    "role": "assistant",
                    "content": "发现产出缺失，重新写入。",
                    "tool_calls": [{
                        "id": "call_3", "type": "function",
                        "function": {"name": "bash",
                                     "arguments": json.dumps(
                                         {"cmd": "echo '# smoke test result' > 'findings.md' && echo written"})},
                    }],
                }
            else:
                msg = {"role": "assistant", "content": "任务完成。DONE"}

        resp = {"choices": [{"message": msg}]}
        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # 静音


def main():
    workdir = os.path.join(ROOT, "_smoke_work")
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)
    workdir_ref[0] = workdir

    srv = http.server.HTTPServer(("127.0.0.1", PORT), MockLLM)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)

    task_file = os.path.join(workdir, "task.md")
    with open(task_file, "w", encoding="utf-8") as f:
        f.write("# 冒烟测试任务\n\n执行两步操作后输出 DONE。\n")

    env = dict(os.environ)
    env["LONGTASK_API"] = "http://127.0.0.1:%d/v1/chat/completions" % PORT
    env["LONGTASK_API_KEY"] = "test-key"
    env["LONGTASK_MODEL"] = "mock-model"
    env["WORK"] = workdir

    print("=" * 60)
    print("端到端冒烟测试")
    print("=" * 60)

    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "runner.py"),
         task_file, workdir, "mock-model", "8"],
        capture_output=True, text=True, timeout=60, env=env,
    )

    print("\n--- runner stdout ---")
    print(result.stdout.strip())

    checks = []

    # 1. 正常终止
    checks.append(("进程正常退出", result.returncode == 0))
    checks.append(("输出 AGENT_FINISHED", "AGENT_FINISHED" in result.stdout))

    # 2. 工具真的被调用了
    log_path = os.path.join(workdir, "agent.log")
    log_text = open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else ""
    checks.append(("日志已生成", os.path.exists(log_path)))
    checks.append(("工具命令已执行并落盘", "echo hello-from-tool" in log_text))

    # 3. 产出文件真的被写入
    findings = os.path.join(workdir, "findings.md")
    checks.append(("产出文件已写入", os.path.exists(findings)))
    if os.path.exists(findings):
        content = open(findings, encoding="utf-8").read()
        checks.append(("产出内容正确", "smoke test result" in content))

    # 4. 请求协议正确（消息角色序列合法）
    roles_ok = True
    for req in SEEN_REQUESTS:
        msgs = req.get("messages", [])
        tool_ids = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
        call_ids = set()
        for m in msgs:
            for tc in (m.get("tool_calls") or []):
                call_ids.add(tc.get("id"))
        if tool_ids - call_ids:
            roles_ok = False
    checks.append(("请求消息配对合法", roles_ok))
    checks.append(("模型与工具名正确", SEEN_REQUESTS and
                   SEEN_REQUESTS[0].get("model") == "mock-model" and
                   SEEN_REQUESTS[0].get("tools", [{}])[0].get("function", {}).get("name") == "bash"))

    print("\n--- 检查项 ---")
    failed = 0
    for name, ok in checks:
        print("  %s %s" % ("✅" if ok else "❌", name))
        if not ok:
            failed += 1

    srv.shutdown()

    # 失败时保留现场，便于排查
    if failed:
        print("\n(工作目录已保留：%s)" % workdir)
        log_path = os.path.join(workdir, "agent.log")
        if os.path.exists(log_path):
            print("\n--- agent.log 尾部 ---")
            print("\n".join(open(log_path, encoding="utf-8").read().splitlines()[-15:]))
        print("\n--- 工作目录内容 ---")
        for dp, dn, fn in os.walk(workdir):
            for f in fn:
                print("  ", os.path.join(dp, f))
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 60)
    if failed:
        print("❌ 冒烟测试失败：%d 项未通过" % failed)
        return 1
    print("✅ 冒烟测试全部通过（%d/%d）" % (len(checks), len(checks)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
