"""
图灵测试 · 语言习惯学习
借鉴 MaiBot 的 learn_style / learn_jargon：从对方发言中学习口吻与口头禅
"""
import json
from app.ai.llm import chat_completion
from app.logger import learn_log, error_log


def _extract_json(text: str) -> dict:
    """从模型输出里尽量抠出一段 JSON（容错：去代码块、取首个 {...}）"""
    try:
        s = text.strip()
        if s.startswith("```"):
            parts = s.split("```")
            s = parts[1] if len(parts) > 1 else s
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        return json.loads(s[start:end + 1])
    except Exception:
        return {}


async def learn(user_messages: list) -> tuple:
    """
    从用户发言中学习语言风格和口头禅
    返回 (expressions: list, jargon: list)
    - expressions: ["当『X』时，可以用『Y』", ...]
    - jargon: ["口头禅1", "口头禅2", ...]
    """
    recent = user_messages[-8:]
    if len(recent) < 2:
        return [], []

    chat_str = "\n".join(f"用户：{m}" for m in recent)
    sys_prompt = (
        "下面是某用户和别人的聊天记录（只含该用户自己的发言）：\n"
        f"{chat_str}\n\n"
        "请只从该用户的发言中总结其『怎么说话』，不要总结 AI 或他人的话。\n"
        "要求：\n"
        "1. 提取 2-4 条语言风格规律，格式：situation 是场景(≤15字)，style 是对应的语气/句式/表达(≤15字)，"
        "例如 situation=『表示惊叹』, style=『用 我嘞个xxx』。\n"
        "2. 提取该用户反复使用的口头禅或习惯用词（1-4 个）。\n"
        "3. 只输出 JSON，不要其他内容："
        '{"expressions":[{"situation":"...","style":"..."}],"jargon":["..."]}\n'
        "4. 不要涉及具体人名、地名、具体事件，只关注语言习惯本身。\n"
        "5. 不要提取脏话（sb、傻逼等）、单字符、纯标点作为口头禅。\n"
        "6. 口头禅应该是通用的语气助词或网络用语（如'哈哈哈''笑死'），不是侮辱性词汇。"
    )
    try:
        out = await chat_completion(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "请输出 JSON。"}],
            max_tokens_override=150,
        )
        data = _extract_json(out)
        exprs = data.get("expressions") or []
        jargon = data.get("jargon") or []
        expressions = [
            (e.get("situation", ""), e.get("style", ""))
            for e in exprs
            if isinstance(e, dict) and e.get("situation") and e.get("style")
        ][:4]
        jargon_list = [str(j) for j in jargon if j][:4]
        return expressions, jargon_list
    except Exception as e:
        error_log("Learn", str(e))
        return [], []
