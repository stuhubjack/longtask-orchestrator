# 续跑示例

> 演示怎么用一个干净上下文恢复中断的长任务。
> 用法：`python3 core/runner.py examples/demo-task.md work gpt-4o-mini 60 examples/seed-example.md`

# 续跑说明

## 已完成

- 项目 A（Node.js 生态）调研完成，原始数据在 `artifacts/s01/repo_a.json`
- 关键结论：最近提交活跃，依赖数 42 个，未修复 issue 17 条
- 项目 B（Python 生态）调研进行到一半，部分数据在 `artifacts/s02/partial.json`

## 当前进度

- 下一步：补齐项目 B 的「未修复 issue 数量」与「License」两项
- 然后开始项目 C（Go 生态）

## 下一步动作

1. 读 `artifacts/s02/partial.json` 看已有什么
2. 补查项目 B 缺失的两项
3. 调研项目 C
4. 汇总写入 `work/findings.md`

## 已知约束

- GitHub API 未认证时限速 60 次/小时，**先读已有文件再决定要不要发请求**
- 上一轮遇到过一次超时，重试后正常——超时不要立即判失败
- 报告里每项数据都要带采集时间
