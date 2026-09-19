#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脱敏扫描器 —— 发布前检查

在把任何内容推到公开仓库前跑一遍，拦截不该出现的字符串。

用法:
    python3 scripts/sanitize_check.py <待检查目录>

退出码:
    0 = 通过
    1 = 发现问题（详见输出）
"""
import os
import re
import sys

# 规则：(名称, 正则, 说明)
RULES = [
    # ---- 凭据类：最高优先级 ----
    ("私钥/证书块", r"-----BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----",
     "私钥或证书内容，必须移除"),
    ("常见 API Key 前缀", r"\b(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})",
     "硬编码的 API key / token"),
    ("Bearer Token", r"Bearer\s+[A-Za-z0-9._\-]{20,}",
     "硬编码的 Bearer token（示例里请用 <TOKEN>）"),
    ("赋值型密钥", r"(?i)\b(password|passwd|secret|api_?key|access_?key|token)\s*[:=]\s*[\"']?[A-Za-z0-9!@#$%^&*._\-]{8,}",
     "疑似硬编码凭据"),
    ("URL 内嵌凭据", r"://[^/\s:]+:[^/\s@]+@",
     "URL 中内嵌用户名密码（示例请用 <user>:<pass>@）"),

    # ---- 内网/主机类 ----
    ("内网 IP", r"\b(10|172\.(1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b",
     "内网地址，公开仓库中应移除或改为示例网段"),
    ("SSH 连通命令", r"\bsshpass\b|\bssh\s+-[a-z]*\s*\S+@\d+\.\d+\.\d+\.\d+",
     "带主机信息的 SSH 连接串"),

    # ---- 个人信息 ----
    ("中国大陆手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)", "手机号"),
    ("身份证号", r"(?<!\d)\d{17}[\dXx](?!\d)", "身份证号"),
    ("邮箱", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "邮箱地址"),

    # ---- 内部痕迹 ----
    ("本地绝对路径", r"[A-Za-z]:\\\\?(Users|Users\\\\\w+|jack|Opencode)[^\s\"']*",
     "本地绝对路径，泄露机器结构"),
    ("日期+轮次标记", r"20\d{2}-\d{2}-\d{2}\s*[^\s]{0,20}\s*R\d+\s*(实测|实证|实锤|复盘)",
     "内部工作记录痕迹（含日期与轮次）"),
    ("企业/项目特指", r"(某公司|XX企业|目标企业名)", "指向具体实体的痕迹"),
]

# 明确允许的例外（避免误报）
ALLOWLIST = [
    r"example\.com",
    r"example\.org",
    r"test@example",
    r"user@example",
    r"192\.168\.1\.100",   # 文档中的示例地址
    r"127\.0\.0\.1",
    r"0\.0\.0\.0",
    r"<[A-Z_]+>",          # 占位符如 <TOKEN>
    r"\$\{[A-Z_]+\}",      # 环境变量引用
    r"os\.environ\.get",   # 从环境变量读取 = 正确做法，不是硬编码
    r"os\.getenv",
    # 下列是技术写作中的中性用词，不是内部痕迹
    r"(踩坑|实证|复盘|实测)(记录|经验|教训|结论)?",
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".whl", ".pyc"}


def is_allowed(text):
    return any(re.search(p, text, re.I) for p in ALLOWLIST)


def scan_file(path):
    findings = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:  # noqa: BLE001
        return findings

    for lineno, line in enumerate(lines, 1):
        if line.strip().startswith("#") and "sanitize" in line.lower():
            continue  # 本文件自身的规则定义豁免
        for name, pattern, desc in RULES:
            for m in re.finditer(pattern, line):
                snippet = m.group(0)
                if is_allowed(snippet):
                    continue
                findings.append((path, lineno, name, snippet[:80], desc))
    return findings


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    all_findings = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            fp = os.path.join(dirpath, fn)
            # 跳过本脚本自身，否则规则字符串会自匹配
            if os.path.abspath(fp) == os.path.abspath(__file__):
                continue
            all_findings.extend(scan_file(fp))

    if not all_findings:
        print("✅ 脱敏检查通过：未发现敏感内容")
        return 0

    print("⚠️  发现 %d 处待处理内容：\n" % len(all_findings))
    current = None
    for path, lineno, name, snippet, desc in sorted(all_findings):
        if path != current:
            print("\n📄 %s" % path)
            current = path
        print("   L%-4d [%s] %s" % (lineno, name, snippet))
        print("         ↳ %s" % desc)

    print("\n" + "=" * 60)
    print("处理建议：")
    print("  · 凭据类：直接删除，改用环境变量或 <PLACEHOLDER>")
    print("  · 内网地址：改为 example.com / 192.168.1.100 等文档示例")
    print("  · 个人信息：删除或匿名化")
    print("  · 内部痕迹：改写为中性技术描述（去掉日期/轮次/企业名）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
