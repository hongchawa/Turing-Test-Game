# 图灵测试 (Turing Test Game)

一个在线版的图灵测试互动游戏。你与陌生人聊天，尝试判断对方是真人还是 AI；反之亦然。

## 功能

- 🎮 **双人对战**：随机匹配陌生人，通过文字聊天判断对方是否为 AI
- 👥 **多人模式**：支持多人房间同时参与
- 🤖 **AI 对手**：内置 AI 模型模拟真人对话
- 👀 **观战系统**：管理员可旁观对战房间
- ⚙️ **管理后台**：实时调整 AI 配置、系统设置

## 快速开始

```bash
# 1. 复制配置模板并填入你的 API 密钥
cp config.example.json config.json

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动服务
python run.py
```

打开浏览器访问 `http://localhost:8000` 即可。

## 技术栈

- **后端**: Python / FastAPI + WebSocket
- **前端**: 原生 HTML / CSS / JavaScript
- **AI**: 接入 NVIDIA / OpenAI 兼容 API
- **数据库**: SQLite

## 许可证

[MIT](LICENSE)
