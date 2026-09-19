# 演示视频

## 为什么视频不放仓库里

| 问题 | 说明 |
|------|------|
| **100 MB 硬上限** | GitHub 单文件超过 100 MB 会直接拒绝推送 |
| **clone 变慢** | 视频在 repo 里，每个 clone 的人都得完整拉下来 |
| **仓库膨胀** | git 对大二进制文件每次改动都存完整副本，历史会迅速变大 |

**推荐方案**：视频作为 **Release 附件**托管，README 只放封面图 + 链接。

Release 附件没有 100 MB 限制（单文件上限 2 GB），且不进入 git 历史。

---

## 上传步骤

### 1. 先确认仓库已发布

如果还没推送，先在 GitHub Desktop 里 `Publish repository`（记得勾私有）。

### 2. 创建 Release 并上传视频

在浏览器打开：

```
https://github.com/stuhubjack/longtask-orchestrator/releases/new
```

填写：

| 字段 | 值 |
|------|-----|
| **Choose a tag** | 输入 `v0.1.0` → 点 `Create new tag` |
| **Release title** | `v0.1.0 演示` |
| **Describe** | `长任务编排框架演示视频` |
| **Attach binaries** | 把你的视频文件拖进这个区域 |

点 `Publish release`。

### 3. 复制视频的真实地址

发布后，在 Release 页面右键点击附件视频 → `复制链接地址`，
形如：

```
https://github.com/stuhubjack/longtask-orchestrator/releases/download/v0.1.0/demo.mp4
```

### 4. 让 README 直接嵌入播放

GitHub 的 Markdown **支持从 Release 附件嵌入视频**。编辑 README，把演示区改成：

```html
<div align="center">

https://github.com/stuhubjack/longtask-orchestrator/releases/download/v0.1.0/demo.mp4

</div>
```

> 把裸链接单独放一行，GitHub 会自动渲染成视频播放器。
> （这是 GitHub 的特有行为，直接写 URL 就能播放视频）

---

## 封面图怎么换

README 引用的是 `docs/assets/demo-cover.png`。

**做法**：从视频里截一帧有代表性的画面（能看到框架跑起来、终端输出最直观），
尺寸建议 **1280×720**，替换掉同名文件即可。

**要求**：
- ⚠️ **截图里不能出现任何真实目标信息**（域名、IP、凭证、企业名）
- 用示例任务跑一遍再截，或截完用马赛克盖掉

替换后：

```bash
cd longtask-orchestrator
git add docs/assets/demo-cover.png
git commit -m "docs: 更新演示封面"
git push
```

---

## 视频录制建议

如果要**重录一个适合公开的演示**（推荐，比裁剪现有录像干净）：

```bash
# 1. 起一个示例任务
export LONGTASK_API="..."
export LONGTASK_API_KEY="..."
export LONGTASK_MODEL="..."

python3 core/runner.py examples/demo-task.md work
```

录制内容建议顺序：

1. **跑冒烟测试**（30 秒）——`python3 scripts/smoke_test.py`，8/8 亮绿，最直观
2. **实际跑一次任务**（1-2 分钟）——用 `examples/demo-task.md`，展示逐轮决策
3. **看产出**（20 秒）——`cat work/findings.md`，证明真有东西落盘
4. **看日志**（20 秒）——`cat work/agent.log`，展示可回放性

总时长 **2-3 分钟**足够，越短越好。

**录制注意**：
- 终端窗口调大字号，录出来能看清
- 不要录到桌面其他文件、通知弹窗、聊天窗口
- 别用真实 API key 的画面（env 命令那一行要么不录，要么提前 export 好）

---

## 视频存哪（如果不走 GitHub）

也可以用外部平台，README 里换成对应嵌入：

| 平台 | 适合 | 注意 |
|------|------|------|
| **GitHub Release** | 首选，和项目在一起 | 无时长限制 |
| B 站 | 国内访问快 | 需账号，可能被投币/评论 |
| YouTube | 国际受众 | 你的网络环境可能不便管理 |
| asciinema | **终端录制**，最适合本项目 | 纯文本、体积极小、可复制命令 |

> 💡 **强烈推荐考虑 asciinema**：它录的是终端操作，输出是一个几百 KB 的
> 文本文件，可以直接放仓库里，还能在网页上回放、暂停复制命令。
> 对这类 CLI 工具来说比 mp4 更合适。
>
> 安装：`pip install asciinema` → `asciinema rec demo.cast` → 操作 → `exit`
> 上传到 https://asciinema.org 或直接把 `.cast` 文件放仓库。
