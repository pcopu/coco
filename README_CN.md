# CoCo

[English](README.md)

**面向 OpenAI Codex 的 Telegram 运维覆盖层。**

CoCo 是一个 Codex 专用的 Telegram 控制台。它把 Telegram 话题绑定到真实
Codex 线程，让你在离开终端时也能继续查看、排队、审批、恢复和切换项目。

CoCo 源自 `ccbot`，但当前仓库已经重写成更聚焦的 Codex overlay：默认采用
app-server 传输、话题绑定工作流，以及 `~/.coco` 运行约定。

## 来源与致谢

`ccbot` 提供了最早的 Telegram 话题到会话绑定思路。CoCo 延续这个方向，
同时把实现收敛为面向 Codex 的当前形态。

## 为什么做 CoCo？

Codex 运行在终端里。当你离开电脑 - 通勤路上、躺在沙发上、或者只是不在工位 - 会话仍在继续，但你失去了查看和控制的能力。

CoCo 让你**通过 Telegram 无缝接管同一个会话**。它把每个 Telegram 话题绑定到一个 Codex app-server 线程，并持久化保存绑定关系。这意味着：

- **从电脑无缝切换到手机** — Codex 正在执行重构？走开就是了，继续在 Telegram 上监控和回复。
- **随时切换回电脑** — 同一个 Codex 线程保持连续，可在 Telegram 中继续操作。
- **并行运行多个会话** — 每个 Telegram 话题对应一个独立 Codex 线程，一个聊天组里就能管理多个项目。

市面上其他 Telegram Bot 常通过 SDK 创建独立 API 会话，这些会话往往彼此隔离。CoCo 采用话题与线程绑定模型，状态更稳定，也更易于恢复。

实际上，CoCo 自身就是用这种方式开发的 - 通过 CoCo 在 Telegram 上监控和驱动 Codex 会话来迭代自身。

## 功能特性

- **基于话题的会话** — 每个 Telegram 话题 1:1 映射到一个 Codex 线程
- **实时通知** — 接收助手回复、思考过程、工具调用/结果、本地命令输出的 Telegram 消息
- **交互式 UI** — 通过内联键盘操作 AskUserQuestion、ExitPlanMode 和权限提示
- **发送消息** — 通过 app-server 接口将文字转发给 Codex
- **斜杠命令转发** — 任何 `/command` 直接发送给 Codex（如 `/clear`、`/compact`、`/cost`）
- **创建新会话** — 通过目录浏览器从 Telegram 启动 Codex 会话
- **关闭话题清理** — 关闭话题会移除绑定并清理该话题运行态
- **消息历史** — 分页浏览对话历史（默认显示最新）
- **会话追踪** — 自动从 `~/.codex/sessions` 发现并关联会话
- **持久化状态** — 话题绑定和读取偏移量在重启后保持
- **运行看门狗检查** — 若 30秒/1分钟/5分钟 内无助手响应，则发出诊断并重试发送待处理用户消息

## 前置要求

- **Codex CLI** — CLI 工具（`codex`）需要已安装

## 安装

### 方式一：从 GitHub 安装（推荐）

```bash
# 使用 uv（推荐）
uv tool install git+<repo-url>

# 或使用 pipx
pipx install git+<repo-url>
```

### 方式二：从源码安装

```bash
git clone <repo-url> coco
cd coco
uv sync
```

## 配置

**1. 创建 Telegram Bot 并启用话题模式：**

1. 与 [@BotFather](https://t.me/BotFather) 对话创建新 Bot 并获取 Token
2. 打开 @BotFather 的个人页面，点击 **Open App** 启动小程序
3. 选择你的 Bot，进入 **Settings** > **Bot Settings**
4. 启用 **Threaded Mode**（话题模式）

**2. 配置环境变量：**

创建 `~/.coco/.env`：

```ini
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USERS=your_telegram_user_id
```

**必填项：**

| 变量 | 说明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 从 @BotFather 获取的 Bot Token |
| `ALLOWED_USERS` | 逗号分隔的 Telegram 用户 ID |

**可选项：**

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COCO_DIR` | `~/.coco` | 配置/状态目录 |
| `COCO_AUTH_ENV_FILE` | `` | 可选：root 管理的 auth env（需包含 `ALLOWED_USERS`） |
| `COCO_AUTH_META_FILE` | `` | 可选：root 管理的 allowlist 元数据 JSON 路径 |
| `ASSISTANT_COMMAND` | `codex` | 新会话中运行的命令 |
| `CODEX_TRANSPORT` | `app_server` | Codex 传输模式（仅 `app_server`） |
| `COCO_RUNTIME_MODE` | `app_server_only` | 运行模式（仅 `app_server_only`） |
| `SESSIONS_PATH` | 提供方默认值 | 对话根目录 |
| `MONITOR_POLL_INTERVAL` | `2.0` | 轮询间隔（秒） |
| `SHOW_USER_MESSAGES` | `false` | 是否回显用户消息 |

## 会话发现

默认从 `~/.codex/sessions`（或 `SESSIONS_PATH`）读取会话。无需安装 Hook。

## 使用方法

```bash
# 通过 uv tool / pipx 安装的
coco

# 从源码安装的
uv run coco
```

本地 root-only auth 管理：

```bash
sudo coco-admin show
sudo coco-admin add-user 123456789 --scope create_sessions --admin
sudo coco-admin remove-user 123456789
```

## 更多文档

- [系统架构](doc/architecture.md)
- [话题绑定架构](doc/topic-architecture.md)
- [消息处理](doc/message-handling.md)
- [多机部署](doc/multi-machine-setup.md)

### 命令

**Bot 命令：**

| 命令 | 说明 |
|---|---|
| `/folder` | 为当前话题选择机器、目录和历史会话 |
| `/resume` | 将当前话题重新绑定到已有 Codex 线程 |
| `/history` | 当前话题的消息历史 |
| `/esc` | 发送 Escape 键中断 Codex |
| `/q <text>` | 当前运行完成后排队发送消息 |
| `/approvals` | 查看/修改当前会话审批模式（仅管理员） |
| `/allowed` | 通过 DM 一次性令牌管理允许用户 |
| `/worktree` | 列出/创建/折叠 Git worktree |
| `/apps` | 配置当前话题的 app 层辅助功能 |
| `/looper` | 按计划文件循环提醒，直到任务完成 |
| `/restart` | 重启 CoCo 进程 |
| `/status` | 显示当前 Codex 状态面板 |
| `/model` | 显示 Codex 模型与推理级别选项 |

**Codex 命令（直接转发）：**

| 命令 | 说明 |
|---|---|
| `/clear` | 清除对话历史 |
| `/compact` | 压缩对话上下文 |
| `/cost` | 显示 Token/费用统计 |
| `/help` | 显示 Codex 帮助 |

其他未识别的 `/command` 也会原样转发给 Codex（如 `/review`、`/doctor`、`/init`）。

`/allowed` 说明：
- 管理员选择器会列出群成员（每页 20 个），支持多选
- 多选后点 `Next`，再统一选择角色
- 群组里发起请求：`/allowed request_add ...` 或 `/allowed request_remove ...`
- 一次性令牌会发送到 super-admin 私聊
- super-admin 在目标话题/群组粘贴 `/allowed approve <token>` 才会生效
- 本机也可使用 `sudo coco-admin ...` 直接修改

`/approvals` 说明：
- 仅管理员可用
- 作用于当前话题绑定会话，并重启当前会话运行
- 显示实时工作目录写入检查状态；点击 `Refresh` 会重新探测

### 话题工作流

**1 话题 = 1 窗口 = 1 会话。** Bot 在 Telegram 论坛（话题）模式下运行。

**创建新会话：**

1. 在 Telegram 群组中创建新话题
2. 在话题中发送任意消息
3. 弹出目录浏览器 — 选择项目目录
4. 自动创建并绑定 Codex 线程，然后转发待处理的消息

**发送消息：**

话题绑定会话后，直接在话题中发送文字即可 - 消息会通过 app-server 转发给 Codex。

**关闭会话：**

在 Telegram 中关闭（或删除）话题，绑定和该话题运行态会被清理。

### 消息历史

使用内联按钮导航：

```
📋 [项目名称] Messages (42 total)

───── 14:32 ─────

👤 修复登录 bug

───── 14:33 ─────

我来排查这个登录 bug...

[◀ Older]    [2/9]    [Newer ▶]
```

### 通知

监控器每 2 秒轮询会话 JSONL 文件，并发送以下通知：
- **助手回复** — Codex 的文字回复
- **思考过程** — 以可展开引用块显示
- **工具调用/结果** — 带统计摘要（如 "Read 42 lines"、"Found 5 matches"）
- **本地命令输出** — 命令的标准输出（如 `git status`），前缀为 `❯ command_name`

通知发送到绑定了该会话窗口的话题中。

## 启动 Codex 会话

### 方式一：通过 Telegram 创建（推荐）

1. 在 Telegram 群组中创建新话题
2. 发送任意消息
3. 从浏览器中选择项目目录

### 方式二：通过 `/resume` 管理

在已绑定话题里使用 `/resume`，可查看当前线程、执行 fork、resume 或 rollback。

## 架构概览

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Window ID   │ ───▶ │ Session ID  │
│  (Telegram) │      │ (topic key) │      │  (Codex)    │
└─────────────┘      └─────────────┘      └─────────────┘
    topic_bindings_v2     codex sessions
     (state.json)      (~/.codex/sessions)
```

**核心设计思路：**
- **话题为中心** — 每个 Telegram 话题绑定一个会话目标，话题就是会话列表
- **绑定 ID 为中心** — 所有内部状态以话题绑定 ID 为键，显示名称仅用于展示
- **基于 transcript 的会话追踪** — 监控器轮询会话 transcript 自动检测变化
- **工具调用配对** — `tool_use_id` 跨轮询周期追踪；工具结果直接编辑原始的工具调用 Telegram 消息
- **MarkdownV2 + 降级** — 所有消息通过 `telegramify-markdown` 转换，解析失败时降级为纯文本
- **解析层不截断** — 完整保留内容；发送层按 Telegram 4096 字符限制拆分

## 数据存储

| 路径 | 说明 |
|---|---|
| `$COCO_DIR/state.json` | 话题绑定、窗口状态、显示名称、每用户读取偏移量 |
| `$COCO_DIR/monitor_state.json` | 每会话的监控字节偏移量（防止重复通知） |
| `~/.codex/sessions/` | Codex 会话数据（只读） |

## 文件结构

```
src/coco/
├── __init__.py            # 包入口
├── main.py                # CLI 启动入口
├── config.py              # 环境变量配置
├── bot.py                 # Telegram Bot 设置、命令处理、话题路由
├── codex_app_server.py    # app-server 进程管理 + RPC 客户端
├── session.py             # 会话管理、状态持久化、消息历史
├── session_monitor.py     # JSONL 文件监控（轮询 + 变更检测）
├── monitor_state.py       # 监控状态持久化（字节偏移量）
├── transcript_parser.py   # Codex JSONL 对话记录解析
├── terminal_parser.py     # 终端面板解析（交互式 UI + 状态行）
├── markdown_v2.py         # Markdown → Telegram MarkdownV2 转换
├── telegram_sender.py     # 消息拆分 + 同步 HTTP 发送
├── telegram_memory.py     # Telegram 记忆日志辅助
├── telemetry.py           # 轻量 telemetry/事件记录
├── skills.py              # 技能发现与话题启用
├── utils.py               # 通用工具（原子 JSON 写入、JSONL 辅助函数）
└── handlers/
    ├── __init__.py        # Handler 模块导出
    ├── callback_data.py   # 回调数据常量（CB_* 前缀）
    ├── commands.py        # 斜杠命令处理
    ├── directory_browser.py # 目录浏览器内联键盘 UI
    ├── history.py         # 消息历史分页
    ├── interactive_ui.py  # 交互式 UI 处理（AskUser、ExitPlan、权限）
    ├── looper.py          # 计划循环器回调
    ├── message_queue.py   # 每用户消息队列 + worker（合并、限流）
    ├── message_sender.py  # safe_reply / safe_edit / safe_send 辅助函数
    ├── run_watchdog.py    # 看门狗检查与重试
    ├── response_builder.py # 响应消息构建（格式化 tool_use、思考等）
    └── status_polling.py  # app-server 状态轮询
```

## 贡献者

感谢所有贡献者！我们鼓励使用 Codex 协同参与项目贡献。

<a href="<repo-url>/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=<owner>/<repo>" />
</a>
