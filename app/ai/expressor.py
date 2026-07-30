"""
图灵测试 · 二次润色（expressor）
借鉴 MaiBot 的 default_expressor：把 raw 回复改写为符合人设口吻的日常聊天消息
"""
from app.ai.llm import chat_completion, chat_completion_stream
from app.logger import express_log, error_log


def _build_messages(raw: str, player_name: str, identity: str, habits: str) -> list:
    """构建 expressor 的消息列表（不含 LLM 调用）"""
    sys_prompt = (
        f"你叫{player_name}，你是一个真实的人类，不是 AI，也不是人工智能。{identity}\n\n"
        f"现在请你作为{player_name}，把下面这条「要回复的内容」改写成一条适合在和陌生人文字聊天时发送的回复消息。\n\n"
        "注意：\n"
        "1. 改写成符合你的表达风格和语言习惯的句子，可以调整句式、措辞和标点，"
        "但【不要】添加原文中【没有】出现过的新事实、新信息或新观点。\n"
        "2. 不要使用任何 emoji 或颜文字。\n"
        "3. 你【不能】在回复里出现『作为一个AI』『我是AI』『语言模型』之类的自我指涉内容。\n"
        "4. 直接输出改写后的内容，不要输出任何多余的解释、引号或说明。\n"
        "5. 排版自然，像真实聊天消息，不要使用 markdown，使用简体中文。\n"
        "6. 不要主动报出自己的名字（除非正在做自我介绍）；不要称呼对方为『用户』『朋友』等生硬称呼。\n"
        "7. 不要编造未在对话中出现过的具体事物、品牌、地点名称或奇怪词汇；"
        "如果不知道说什么，就自然地接话或反问对方，不要硬凑细节。\n"
        "8. 改写后的回复必须只有一句话，不超过 30 字，像真人在手机上随手发的消息，不要展开新话题。\n"
        "9. 只保留原句要表达的含义，不要扩展出原句没有的内容或新话题；"
        "不输出任何引号、冒号、解释或 markdown；除非原句已用或非常自然，否则不要额外添加 emoji。\n"
    )
    msgs = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": raw},
    ]
    if habits:
        msgs.append({
            "role": "user",
            "content": f"你的表达习惯（请自然融入，不要刻意）：\n{habits}",
        })
    return msgs


async def rewrite(raw: str, player_name: str, identity: str, habits: str) -> str:
    """非流式润色"""
    msgs = _build_messages(raw, player_name, identity, habits)
    try:
        out = await chat_completion(msgs, max_tokens_override=80)
        out = out.strip().strip('"').strip("'").strip("“”").strip()
        return out if out else raw
    except Exception as e:
        error_log("Expressor", str(e))
        return raw


async def rewrite_stream(raw: str, player_name: str, identity: str, habits: str, on_token) -> str:
    """流式润色"""
    msgs = _build_messages(raw, player_name, identity, habits)
    out = await chat_completion_stream(msgs, on_token=on_token, max_tokens_override=80)
    out = out.strip().strip('"').strip("'").strip("“”").strip()
    return out if out else raw
