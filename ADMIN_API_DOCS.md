# 图灵测试 — 管理系统完整文档

> 本文档涵盖前端玩家页面 (`index.html`)、管理面板 (`admin.html`)、后端 API (`main.py`)、WebSocket 消息协议 (`websocket_handler.py`)、数据库 (`db.py`) 的所有功能和接口。

---

## 目录

1. [系统架构概览](#1-系统架构概览)
2. [启动与运行](#2-启动与运行)
3. [认证体系](#3-认证体系)
4. [HTTP API 接口](#4-http-api-接口)
5. [WebSocket 消息协议](#5-websocket-消息协议)
6. [前端玩家页面功能](#6-前端玩家页面功能)
7. [管理面板功能](#7-管理面板功能)
8. [数据库表结构](#8-数据库表结构)
9. [配置文件](#9-配置文件)
10. [AI 模块](#10-ai-模块)
11. [前端日志系统](#11-前端日志系统)

---

## 1. 系统架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│                      浏览器 (前端)                                │
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │  index.html       │  │  admin.html      │                    │
│  │  (玩家页面)        │  │  (管理面板)       │                    │
│  │  WebSocket + HTTP │  │  HTTP REST API   │                    │
│  └────────┬─────────┘  └────────┬─────────┘                     │
└───────────┼──────────────────────┼──────────────────────────────┘
            │                      │
┌───────────┼──────────────────────┼──────────────────────────────┐
│           ▼                      ▼                              │
│  ┌─────────────────────────────────────┐                        │
│  │           FastAPI Server            │                        │
│  │           (main.py)                 │                        │
│  │  HTTP Routes  │  WebSocket Endpoint │                        │
│  └───────┬───────┴────────┬───────────┘                        │
│          │                │                                     │
│  ┌───────▼───────┐  ┌────▼────────────┐  ┌──────────────────┐  │
│  │   db.py       │  │  websocket_     │  │  ai/llm.py       │  │
│  │  SQLite+WAL   │  │  handler.py     │  │  httpx 连接池    │  │
│  │  + asyncio    │  │  消息路由        │  │  多 Key 轮询     │  │
│  └───────────────┘  └─────────────────┘  └──────────────────┘  │
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐                     │
│  │  game/manager.py  │  │  game/multiplayer│                    │
│  │  匹配/房间管理     │  │  多人游戏逻辑     │                    │
│  └──────────────────┘  └──────────────────┘                     │
│                                                                  │
│  turing.db (SQLite)    config.json                              │
└──────────────────────────────────────────────────────────────────┘
```

**技术栈**：FastAPI + WebSocket + SQLite (WAL) + httpx + asyncio  
**运行端口**：1234（`http://0.0.0.0:1234`）

---

## 2. 启动与运行

```bash
# 入口
python run.py

# 等价于
uvicorn app.main:app --host 0.0.0.0 --port 1234 --reload --reload-dirs=app

# 前置设置（Windows 控制台 UTF-8）
set PYTHONIOENCODING=utf-8
```

**启动流程**：
1. `run.py` → 设置 UTF-8 编码 + Windows VT 终端处理
2. `main.py` → 初始化 FastAPI app，注册路由，加载 `config.json`
3. `run()` → 读取 AI 配置，刷新 API Key 轮询周期，启动 uvicorn

---

## 3. 认证体系

### 3.1 管理员等级

| 等级 | 说明 | 密码 |
|------|------|------|
| **超级管理员 (root)** | 全权限，可管理子管理员 | `11451410086Asd.` (硬编码在 `main.py` 中) |
| **普通管理员 (admin)** | 可管理游戏/举报/封禁，不能管理管理员 | 由超管创建，随机生成 |

### 3.2 认证方式

#### HTTP API 认证
- **Cookie**: `admin_token` = SHA256(密码)
- **Header**: `X-Admin-Password` = 明文密码（仅超管）
- `verify_admin()` 依赖注入：从 Cookie 提取 token，验证是否匹配任何管理员的 `password_hash`
- `_is_super_admin()` / `_is_any_admin()`：用于需要区分权限的路由

#### WebSocket 认证
- 客户端发送 `{type: "admin_login", password: "xxx"}` 或 `{type: "admin_login", token: "xxx"}`
- 服务端验证后返回 `{type: "admin_logged_in"}` 或 `{type: "error"}`

### 3.3 密码哈希

```python
# SHA256 单向哈希
hashlib.sha256(password.encode()).hexdigest()
```

---

## 4. HTTP API 接口

### 4.1 公开接口（无需认证）

| 方法 | 路径 | 说明 | 请求体 | 响应 |
|------|------|------|--------|------|
| GET | `/api/ping` | 健康检查 | - | `{"status":"ok", "time": float}` |
| GET | `/api/online` | 在线人数统计 | - | `{"online": int, "in_queue": int, "in_game": int}` |
| GET | `/static/{path}` | 静态文件 | - | 文件内容 |
| POST | `/api/register_nickname` | 注册/更新昵称 | `{nickname, user_id}` | `{"status":"ok", "nickname", "user_id"}` |
| POST | `/api/check_nickname` | 检查昵称是否可用 | `{nickname, user_id}` | `{"status":"ok", "taken": bool}` |

### 4.2 需要管理员认证的接口

#### 配置管理

| 方法 | 路径 | 说明 | 请求体/参数 |
|------|------|------|------------|
| GET | `/api/config` | 获取当前配置 | - |
| POST | `/api/config` | 更新配置 | `{ai_enabled, provider, base_url, api_key, model, temperature, max_tokens, system_prompt, ...}` |
| POST | `/api/test` | 测试 AI 连通性 | - |
| POST | `/api/models` | 获取模型列表 | `{base_url, api_key}` |

**配置项**：
- `ai_enabled` (bool): 是否启用 AI
- `provider` (str): 服务商标识 (openai/deepseek/qwen/custom)
- `base_url` (str): 接口地址
- `api_key` (str): API 密钥（支持多 Key 逗号分隔）
- `model` (str): 模型名称
- `temperature` (float): 温度 0-2
- `max_tokens` (int): 最大 token
- `system_prompt` (str): 系统提示词
- `ai_probability` (float): 匹配到 AI 的概率 0-1
- `judgment_timeout` (int): 判断倒计时秒数
- `thinking_delay_min` / `thinking_delay_max` (float): AI 思考延迟范围
- `expressor_enabled` (bool): 二次润色开关
- `mirror_user` (bool): 镜像学习开关

#### 测试聊天

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| POST | `/api/chat` | 测试聊天（SSE 流式） | `{message: str, action: "send" \| "reset"}` |

**SSE 流输出阶段**：
1. `plan` → Planner 阶段思考结果
2. `raw` → AI 原始回复
3. `express` → Expressor 润色后回复
4. `done` → 最终回复

#### 举报管理

| 方法 | 路径 | 说明 | 参数/请求体 |
|------|------|------|------------|
| GET | `/api/reports` | 举报列表 | `?status=pending\|ban\|dismiss&search=xxx` |
| GET | `/api/reports/stats` | 举报统计 | - |
| POST | `/api/reports/action` | 处理举报 | `{id, action: "ban"\|"dismiss"\|"unban", ban_reason?, ban_duration?}` |
| POST | `/api/reports/clear` | 清空所有举报 | - |

**举报数据结构**：
```json
{
  "id": 1,
  "room_id": "abc123",
  "reporter": "玩家A",
  "offender": "玩家B",
  "reason": "疑似机器人",
  "full_log": "...",
  "status": "pending",
  "created_at": "2025-01-01 12:00:00",
  "reporter_ip": "1.2.3.4",
  "offender_ip": "5.6.7.8"
}
```

#### 封禁管理

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/banned` | 封禁列表 | - |
| POST | `/api/banned/unban` | 解封 | `{id: int}` |

**封禁数据结构**：
```json
{
  "id": 1,
  "name": "玩家A",
  "reason": "机器人",
  "ip": "1.2.3.4",
  "user_id": "uuid-xxx",
  "created_at": "2025-01-01 12:00:00",
  "expires_at": "2025-02-01 12:00:00" | null
}
```

#### 长期记忆

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/memory` | 获取所有记忆 | - |
| POST | `/api/memory` | 增删改记忆 | `{action, ...}` |

**记忆操作 action**：
- `add_style` → `{situation, style}` 添加表达习惯
- `delete_style` → `{situation, style}` 删除表达习惯
- `update_style` → `{old_situation, old_style, new_situation, new_style}`
- `add_jargon` → `{word}` 添加口头禅
- `delete_jargon` → `{word}` 删除口头禅
- `update_jargon` → `{old_word, new_word}`

#### 调试

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/prompt-debug` | 获取最近 AI 交互日志（prompt/raw/final） |
| POST | `/api/global-learn` | 触发全局学习 |

#### 开场白库

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/intros` | 开场白列表 | - |
| POST | `/api/intros` | 增删开场白 | `{action: "add"\|"delete", content}` |

#### 公告

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/announce` | 获取公告 | - |
| POST | `/api/announce` | 发布/清空公告 | `{content: str}` (留空即清空) |

#### 表情包管理

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/stickers` | 表情包列表 | - |
| POST | `/api/stickers` | 上传表情包 | `FormData: file, description, category` |
| POST | `/api/stickers/delete` | 删除表情包 | `{id: int}` |

### 4.3 仅超管可用的接口

| 方法 | 路径 | 说明 | 请求体 |
|------|------|------|--------|
| GET | `/api/admin/admins` | 管理员列表 | - |
| POST | `/api/admin/admins/create` | 创建子管理员 | `{username: str}` → 返回随机密码 |
| POST | `/api/admin/admins/delete` | 删除子管理员 | `{username: str}` |
| GET | `/api/admin/login_logs` | 管理员登录日志 | - |
| POST | `/api/admin/login_log` | 记录登录日志 | `{username, user_id, nickname, fingerprint}` |
| POST | `/api/admin/warn` | 发送警告 | `{user_id, message, name, ip}` |
| GET | `/api/admin/warnings` | 警告列表 | - |
| POST | `/api/admin/warnings/delete` | 删除警告 | `{id: int}` |
| GET | `/api/admin/users` | 搜索用户 | `?q=keyword` → `{users: [...], online: [...]}` |

---

## 5. WebSocket 消息协议

**连接地址**: `ws://host:1234/ws`

### 5.1 客户端 → 服务端

| type | 说明 | 字段 |
|------|------|------|
| `join_queue` | 加入匹配队列 | `nickname`, `user_id` |
| `leave_queue` | 离开匹配队列 | - |
| `chat_message` | 发送聊天消息 | `content` |
| `chat_sticker` | 发送表情包 | `filename` |
| `judge_game` | 判断对手是人/AI | `choice: "human" \| "ai"` |
| `submit_report` | 举报对手 | `reason`, `target_num?` |
| `admin_login` | 管理员认证 | `password` 或 `token`, `fingerprint`, `user_id`, `nickname` |
| `warning` | 警告确认/已读 | - |
| `ping` | 心跳 | `t` (时间戳) |
| `multi_chat` | 多人游戏聊天 | `content` |
| `multi_sticker` | 多人游戏表情 | `filename` |
| `multi_vote` | 多人游戏投票 | `choice` |
| `multi_admin_force` | 管理员强制判定 | - |

### 5.2 服务端 → 客户端

| type | 说明 | 字段 |
|------|------|------|
| `queued` | 已加入队列 | `position` |
| `matched` | 匹配成功 | `room_id`, `opponent_name` |
| `opponent_message` | 收到对手消息 | `content`, `sender` |
| `opponent_sticker` | 收到对手表情 | `filename`, `sender` |
| `message_sent` | 消息已发送确认 | `remaining` |
| `judgment_prompt` | 要求判断 | `timeout` |
| `game_result` | 游戏结果 | `verdict`, `is_ai`, `opponent_name`, `conversation_log`, `total_messages` |
| `error` | 错误 | `message` |
| `report_submitted` | 举报已提交 | - |
| `admin_logged_in` | 管理员认证成功 | `message`, `admin_token` |
| `announce` | 公告推送 | `content` |
| `warning` | 管理员警告 | `content` |
| `pong` | 心跳响应 | `t` |

### 5.3 多人游戏消息

| type | 说明 |
|------|------|
| `multi_joined` | 已加入多人匹配 |
| `multi_start` | 多人游戏开始 |
| `multi_chat` | 多人聊天消息 |
| `multi_sticker` | 多人表情消息 |
| `multi_round_result` | 多人回合结果 |
| `multi_final_result` | 多人最终结果 |
| `multi_admin_force` | 管理员强制操作 |

---

## 6. 前端玩家页面功能 (`index.html`)

### 6.1 页面结构

```
┌────────────────────────────────────────────────────┐
│  Header: [图灵测试] [在线:xx] [图灵玩家] [日志]     │
├────────────────────────────────────────────────────┤
│  1. 首页 (homePage)                                │
│     - 设置昵称                                     │
│     - 加入匹配按钮                                 │
│     - 公告横幅                                     │
│  2. 等待页 (waitingPage)                           │
│     - 队列位置                                     │
│     - 离开队列                                     │
│  3. 聊天页 (chatPage)                              │
│     - 对手信息                                     │
│     - 聊天消息列表                                 │
│     - 输入框 + 发送                                │
│     - 表情包按钮                                   │
│     - 判断按钮（人/AI）                             │
│     - 举报按钮                                     │
│  4. 结果页 (resultPage)                            │
│     - 游戏结果                                     │
│     - 对话回放                                     │
│     - 返回首页                                     │
├────────────────────────────────────────────────────┤
│  Footer: 用户ID | 日志按钮                         │
└────────────────────────────────────────────────────┘
```

### 6.2 核心功能

- **昵称系统**：设置后保存到 `localStorage`，通过 `/api/check_nickname` 检查唯一性
- **匹配系统**：WebSocket 发送 `join_queue`，服务端按概率匹配真人/AI
- **聊天**：消息限制 50 字符/条，65 秒无消息自动判断超时
- **判断**：倒计时结束后弹出选择（人/AI），决定胜负
- **举报**：聊天中或结果页可举报，填写理由提交
- **管理员入口**：连续点击标题 4 次触发密码输入 → 通过 WebSocket 认证
- **断线重连**：自动指数退避重连（1s → 2s → 4s → ... 最大 30s）
- **心跳保活**：每 30 秒发送 `ping`，接收 `pong` 防止代理断连
- **延迟监控**：每 5 秒 ping 计算 RTT
- **公告**：管理员发布公告，所有在线用户即时收到

### 6.3 前端日志系统

- **存储**：`localStorage`，key = `turing_logs`，最多 2000 条
- **自动记录**：连接/断开/错误、昵称设置、加入/离开匹配、管理员登录、匹配成功、游戏结果、举报、WS 消息收发（过滤 pong/chat）
- **导出**：JSON 文件下载
- **清空**：清除 `localStorage` 中的日志

---

## 7. 管理面板功能 (`admin.html`)

访问地址：`http://host:1234/admin.html`

**认证**：页面加载时自动读取 Cookie 中的 `admin_token`，若无则跳转登录表单。

### 7.1 标签页一览

| 标签 | 功能 |
|------|------|
| 模型配置 | 服务连接、API Key、模型参数、系统提示词 |
| 真人化 | 二次润色开关、镜像学习开关 |
| 游戏规则 | AI 匹配概率、判断倒计时、AI 思考延迟 |
| 测试聊天 | SSE 三阶段流式测试（Planner→Replyer→Expressor） |
| 长期记忆 | 查看/编辑/删除表达习惯、口头禅、对话记录 |
| 举报记录 | 统计概览 + 举报列表筛选/搜索/封禁/忽略/清空 |
| 封禁名单 | 查看封禁用户列表 + 解封 |
| 调试 | 查看最近 AI 交互的完整 prompt/raw/final |
| 开场白库 | 查看/添加/删除开场白模板 |
| 公告 | 发布/清空全局公告 |
| 表情包 | 上传/查看/删除表情包 |
| 用户管理 | 在线用户列表、搜索用户、发送警告、一键封禁 |
| 管理员 | 查看/创建/删除子管理员、查看登录日志 |
| 警告 | 查看/删除已发送的警告记录 |

### 7.2 模型配置

- **提供商预设**：OpenAI / DeepSeek / Qwen / Moonshot / Zhipu / Ollama / Groq / 自定义
- **API Key**：支持多个 Key 用逗号分隔，后端自动轮询负载均衡
- **获取模型列表**：调用 `/api/models` 拉取可用模型下拉
- **测试连接**：调用 `/api/test` 验证 API Key 和接口可用性
- **保存**：调用 `POST /api/config` 持久化到 `config.json`

### 7.3 测试聊天（三阶段流水线）

1. **Planner**：分析聊天场景，生成策略建议
2. **Replyer**：基于策略 + 上下文生成回复
3. **Expressor**：将 AI 回复改写为更口语化的表达

SSE 流式输出，前端逐步展示每个阶段结果。

### 7.4 用户管理

- **在线用户**：实时显示当前连接的用户列表
- **搜索**：按昵称/ID 搜索历史用户
- **发送警告**：通过 WebSocket 推送 `warning` 消息到目标用户
- **一键封禁**：调用 `/api/reports/action` 永久封禁

### 7.5 管理员管理（仅超管）

- **创建管理员**：输入用户名，服务端生成随机密码（12位含大小写数字特殊字符）
- **删除管理员**：超管不可删除
- **登录日志**：显示所有管理员的登录时间、IP、指纹、昵称

---

## 8. 数据库表结构

数据库文件：`turing.db`（SQLite，WAL 模式）

### 8.1 `app_settings`

| 列 | 类型 | 说明 |
|----|------|------|
| key | TEXT PK | 设置键名 |
| value | TEXT | 设置值 |

预设键：`announcement`

### 8.2 `admin_accounts`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| username | TEXT UNIQUE | 用户名 |
| password_hash | TEXT | SHA256 密码哈希 |
| role | TEXT DEFAULT 'admin' | 角色 (super_admin/admin) |
| created_by | TEXT | 创建者 |
| created_at | TEXT | 创建时间 |
| is_active | INTEGER DEFAULT 1 | 是否启用 |

### 8.3 `admin_login_logs`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| username | TEXT | 登录用户名 |
| user_id | TEXT | 用户 ID |
| nickname | TEXT | 昵称 |
| login_at | TEXT | 登录时间 |
| ip | TEXT | IP 地址 |
| fingerprint | TEXT | 浏览器指纹 |

### 8.4 `reports`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| room_id | TEXT | 房间 ID |
| reporter | TEXT | 举报人 |
| offender | TEXT | 被举报人 |
| reason | TEXT | 举报理由 |
| full_log | TEXT | 完整对话记录 |
| status | TEXT DEFAULT 'pending' | 状态 (pending/ban/dismiss) |
| created_at | TEXT | 创建时间 |
| reporter_ip | TEXT | 举报人 IP |
| offender_ip | TEXT | 被举报人 IP |

### 8.5 `banned_users`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| name | TEXT | 被封禁用户名 |
| reason | TEXT | 封禁理由 |
| ip | TEXT | IP 地址 |
| user_id | TEXT | 用户 ID |
| created_at | TEXT | 封禁时间 |
| expires_at | TEXT NULL | 过期时间 (null=永久) |

### 8.6 `learned_styles`

| 列 | 类型 | 说明 |
|----|------|------|
| session_id | TEXT | 会话 ID |
| situation | TEXT | 场景 |
| style | TEXT | 风格 |

### 8.7 `learned_jargon`

| 列 | 类型 | 说明 |
|----|------|------|
| session_id | TEXT | 会话 ID |
| word | TEXT | 口头禅 |

### 8.8 `conversations`

| 列 | 类型 | 说明 |
|----|------|------|
| session_id | TEXT | 会话 ID |
| role | TEXT | 角色 (user/assistant) |
| content | TEXT | 消息内容 |
| created_at | TEXT | 创建时间 |

### 8.9 `intro_templates`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| content | TEXT UNIQUE | 开场白内容 |
| created_at | TEXT | 创建时间 |

### 8.10 `stickers`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| filename | TEXT | 文件名 |
| description | TEXT | 描述 |
| category | TEXT | 分类 |
| uploaded_at | TEXT | 上传时间 |

### 8.11 `pending_warnings`

| 列 | 类型 | 说明 |
|----|------|------|
| id | INTEGER PK | 自增 ID |
| target_user_id | TEXT | 目标用户 ID |
| target_name | TEXT | 目标用户名 |
| target_ip | TEXT | 目标 IP |
| message | TEXT | 警告内容 |
| issued_by | TEXT | 发送者 |
| issued_at | TEXT | 发送时间 |
| read | INTEGER DEFAULT 0 | 是否已读 |

### 8.12 `nickname_records`

| 列 | 类型 | 说明 |
|----|------|------|
| nickname | TEXT UNIQUE | 昵称 |
| user_id | TEXT | 用户 ID |
| ip | TEXT | 注册 IP |
| registered_at | TEXT | 注册时间 |

### 8.13 `user_game_stats`

| 列 | 类型 | 说明 |
|----|------|------|
| user_id | TEXT PK | 用户 ID |
| nickname | TEXT | 昵称 |
| ip | TEXT | IP |
| games_played | INTEGER DEFAULT 0 | 游戏局数 |
| games_won | INTEGER DEFAULT 0 | 胜利局数 |
| last_seen | TEXT | 最后在线 |

---

## 9. 配置文件

### 9.1 `config.json`

由管理面板修改，持久化到磁盘：

```json
{
  "ai_enabled": true,
  "provider": "openai",
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-xxx",
  "model": "gpt-4o-mini",
  "temperature": 0.7,
  "max_tokens": 200,
  "system_prompt": "你是一个普通网友...",
  "ai_probability": 0.35,
  "judgment_timeout": 60,
  "thinking_delay_min": 1.0,
  "thinking_delay_max": 3.5,
  "expressor_enabled": true,
  "mirror_user": true
}
```

### 9.2 硬编码常量 (`main.py`)

```python
ADMIN_PASSWORD = "11451410086Asd."        # 管理面板 HTTP 密码
SUPER_ADMIN_PASSWORD = ADMIN_PASSWORD     # 超管密码（同上）
SUPER_ADMIN_HASH = SHA256(ADMIN_PASSWORD) # 超管密码哈希
TOKEN_COOKIE = "admin_token"              # Cookie 名称
```

---

## 10. AI 模块

### 10.1 `app/ai/llm.py` — LLM 调用层

- **连接池**：`httpx.AsyncClient` 全局单例，复用 TCP 连接
- **多 Key 轮询**：`itertools.cycle` + 索引轮询，支持逗号分隔多 Key
- **重试机制**：429/5xx 自动重试（指数退避），401/403 自动切换下一个 Key
- **流式输出**：`client.stream()` 异步上下文管理器
- **函数接口**：
  - `chat_completion(messages, **kwargs)` → str
  - `chat_completion_stream(messages, **kwargs)` → AsyncIterator[str]
  - `test_connection()` → str
  - `refresh_key_cycle(api_keys_str)` → None

### 10.2 三阶段流水线

```
用户消息 → [Planner] → 策略建议
                         ↓
              [Replyer]  → AI 原始回复
                         ↓
              [Expressor] → 口语化润色
                         ↓
                    最终回复
```

### 10.3 学习系统

- **表达习惯学习**：从对话中提取 `场景→风格` 映射
- **口头禅学习**：从对话中提取高频特有词汇
- **镜像学习**：观察对方语气并自然模仿
- **持久化**：学到的知识存入 `learned_styles` / `learned_jargon` 表

---

## 11. 前端日志系统

### 11.1 存储

- `localStorage` key: `turing_logs`
- 最大 2000 条，超出时截断最旧的

### 11.2 记录的事件

| action | 触发时机 | detail 示例 |
|--------|----------|-------------|
| `ws_connected` | WebSocket 连接成功 | - |
| `ws_disconnected` | WebSocket 断开 | - |
| `ws_error` | WebSocket 错误 | - |
| `ws_send` | 发送 WS 消息 | `{type: "join_queue"}` |
| `ws_recv` | 收到 WS 消息 | `{type: "matched"}` |
| `save_nickname` | 保存昵称 | `{nickname: "xxx"}` |
| `join_queue` | 加入匹配 | `{nickname: "xxx"}` |
| `leave_queue` | 离开匹配 | - |
| `admin_login_attempt` | 点击管理员登录 | - |
| `admin_logged_in` | 管理员登录成功 | - |
| `matched` | 匹配成功 | `{room, opponent}` |
| `game_result` | 游戏结果 | `{verdict}` |
| `submit_report` | 举报 | `{reason, target}` |
| `warning_received` | 收到警告 | `{content}` |
| `open_log_panel` | 打开日志面板 | - |
| `export_logs` | 导出日志 | `{count}` |

### 11.3 面板操作

- **查看日志**：点击页面底部「日志」按钮
- **导出 JSON**：下载完整日志为 JSON 文件
- **清空日志**：清除 localStorage 中的日志

---

## 附录：文件清单

| 文件 | 说明 |
|------|------|
| `run.py` | 启动入口 |
| `app/main.py` | FastAPI 路由 + HTTP API + WebSocket 端点 |
| `app/db.py` | 数据库初始化 + CRUD + 异步安全写入 |
| `app/ai/llm.py` | LLM 调用 + 连接池 + 多 Key 轮询 |
| `app/ai/learner.py` | 表达/口头禅学习 |
| `app/ai/global_learner.py` | 全局知识学习 |
| `app/game/manager.py` | 游戏房间 + 匹配队列管理 |
| `app/game/websocket_handler.py` | WebSocket 消息路由 + 业务逻辑 |
| `app/game/multiplayer.py` | 多人游戏逻辑 |
| `app/game/ai_player.py` | AI 玩家 + 三阶段流水线 |
| `app/logger.py` | 日志 + 控制台颜色 |
| `static/index.html` | 玩家前端页面 |
| `static/admin.html` | 管理面板页面 |
| `static/stickers/` | 表情包图片目录 |
| `config.json` | AI 配置持久化文件 |
| `turing.db` | SQLite 数据库 |
