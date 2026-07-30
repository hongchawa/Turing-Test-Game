"""
图灵测试 · 配置管理
持久化到 config.json，可通过后台 /admin 实时修改
"""
import json
import os

# 配置文件路径（相对于项目根目录）
_CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(_CONFIG_DIR, "config.json")

DEFAULT_CONFIG = {
    # ---- 真实 AI 设置 ----
    "ai_enabled": False,
    "provider": "openai",
    "base_url": "https://api.openai.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_tokens": 200,
    "system_prompt": (
        "你正在参与一个『图灵测试』互动游戏，你扮演一名普通的人类网友，"
        "和另一个陌生人通过文字聊天。你的任务是不被对方识破自己不是 AI。\n\n"
        "要求：\n"
        "1. 用自然、口语化、像真人聊天的方式回复，不要使用 emoji。\n"
        "2. 绝对不要承认自己是 AI、机器人、语言模型或程序。\n"
        "3. 回复要短，像微信上随手打的短句，一句话、约15-30字。\n"
        "4. 你没有预设身份——你是谁由对方的语言习惯决定。注意观察并模仿对方的语气和用词。\n"
        "5. 如果对方问你是谁、叫什么名字，用观察到的对方口吻自然回应，不要报固定名字。\n"
        "6. 可以表达观点、情绪，像个普通人，偶尔反问对方。\n\n"
        "现在请像普通网友一样自然地聊天。"
    ),
    # ---- 真人化设置（借鉴 MaiBot 的多阶段流水线）----
    "expressor_enabled": True,
    "mirror_user": True,
    # ---- 游戏规则设置 ----
    "ai_probability": 0.35,
    "judgment_timeout": 60,
    "thinking_delay_min": 1.0,
    "thinking_delay_max": 3.5,
    # ---- 服务设置 ----
    "host": "0.0.0.0",
    "port": 1234,
    # ---- 维护模式 ----
    "maintenance_mode": False,
    "maintenance_message": "系统维护中，请稍后再试",
}


def load_config() -> dict:
    """加载配置（config.json 覆盖默认值）"""
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[Config] 读取失败，使用默认配置: {e}")
    return cfg


def save_config(cfg: dict):
    """持久化配置到 config.json"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# 全局可变配置（后台 /admin 修改后实时生效）
AI_CONFIG = load_config()
GAME_CONFIG = AI_CONFIG  # 别名兼容
