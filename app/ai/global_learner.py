"""
全局学习器——从所有用户对话中学习常见表达方式和聊天模式
真人+AI都学，但AI的内容标注为反面教材，让AI避免学得像AI
"""
import re
import random
from collections import Counter
from app.db import _get_conn


# 停用词（太常见的词不记录）
STOP_WORDS = {
    "你好", "你", "我", "他", "她", "是", "的", "了", "在", "不", "有", "和", "就", "吗", "呢",
    "啊", "哦", "嗯", "吧", "哈", "呀", "都", "也", "还", "很", "要", "没", "会", "能",
    "这", "那", "什么", "怎么", "一个", "可以", "没有", "不是", "觉得", "知道",
    "哈哈哈", "哈哈哈哈", "嘿嘿", "对", "好", "行", "嗯嗯", "是的",
    "AI", "真人", "人机", "机器人",
}

SLANG_QUALIFIERS = re.compile(r'^[\u4e00-\u9fa5a-zA-Z0-9]{2,8}$')


def _extract_phrases(text: str, min_len: int = 2, max_len: int = 6) -> list:
    """从文本中提取有意义的短语"""
    text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', ' ', text)
    words = [w for w in text.split() if len(w) >= min_len and w not in STOP_WORDS
             and SLANG_QUALIFIERS.match(w)]
    if len(text) <= 12 and len(text) >= 3:
        clean = re.sub(r'\s+', '', text)
        if clean not in STOP_WORDS and 2 <= len(clean) <= 12:
            words.append(clean)
    return words


def train_global_knowledge():
    """扫描所有对话，真人+AI都学，但AI的内容标注为反面教材"""
    conn = _get_conn()
    human_rows = conn.execute(
        "SELECT content FROM conversations WHERE role='user' ORDER BY id"
    ).fetchall()
    ai_rows = conn.execute(
        "SELECT content FROM conversations WHERE role='assistant' ORDER BY id"
    ).fetchall()

    if not human_rows and not ai_rows:
        print("  [GLOBAL LEARN] 无对话数据, 跳过")
        return {"human_slang": [], "human_patterns": [], "ai_slang": [], "ai_patterns": []}

    # --- 学习真人说话 ---
    human_counter = Counter()
    human_messages = [r["content"] for r in human_rows if r["content"]]
    for msg in human_messages:
        for p in _extract_phrases(msg):
            human_counter[p] += 1

    human_common = [(p, c) for p, c in human_counter.items() if c >= 2]
    human_common.sort(key=lambda x: -x[1])
    human_slang = [(p, c) for p, c in human_common if len(p) <= 4 and c >= 3]
    human_expr = [(p, c) for p, c in human_common if len(p) > 4 or (len(p) <= 4 and c < 3)]

    short_human = [m for m in human_messages if 3 <= len(re.sub(r'\s', '', m)) <= 20]
    human_pattern_counter = Counter(short_human)
    human_patterns = [(p, c) for p, c in human_pattern_counter.items()
                      if c >= 2 and not _is_question(p) and p not in STOP_WORDS]
    human_patterns.sort(key=lambda x: -x[1])

    # --- 学习AI说话 (标注为ai_slang/ai_pattern，不要让AI模仿) ---
    ai_counter = Counter()
    ai_messages = [r["content"] for r in ai_rows if r["content"]]
    for msg in ai_messages:
        for p in _extract_phrases(msg):
            ai_counter[p] += 1

    ai_common = [(p, c) for p, c in ai_counter.items() if c >= 2]
    ai_common.sort(key=lambda x: -x[1])
    ai_slang = [(p, c) for p, c in ai_common if len(p) <= 4 and c >= 3]

    short_ai = [m for m in ai_messages if 3 <= len(re.sub(r'\s', '', m)) <= 20]
    ai_pattern_counter = Counter(short_ai)
    ai_patterns = [(p, c) for p, c in ai_pattern_counter.items()
                   if c >= 2 and not _is_question(p) and p not in STOP_WORDS]
    ai_patterns.sort(key=lambda x: -x[1])

    # --- 存入数据库（使用事务避免中途失败导致数据清空）---
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM global_knowledge")
        _save_knowledge(conn, "slang", human_slang[:30])
        _save_knowledge(conn, "expression", human_expr[:20])
        _save_knowledge(conn, "pattern", human_patterns[:15])
        _save_knowledge(conn, "ai_slang", ai_slang[:15])
        _save_knowledge(conn, "ai_pattern", ai_patterns[:10])
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    print(f"  [GLOBAL LEARN] 真人: {len(human_slang[:30])}个口语+{len(human_expr[:20])}个表达+{len(human_patterns[:15])}个模式")
    print(f"  [GLOBAL LEARN] AI  : {len(ai_slang[:15])}个口语+{len(ai_patterns[:10])}个模式 (标注为反面教材)")

    return {
        "human_slang": human_slang[:30],
        "human_patterns": human_patterns[:15],
        "ai_slang": ai_slang[:15],
        "ai_patterns": ai_patterns[:10],
    }


def _is_question(text: str) -> bool:
    return any(q in text for q in ["你是", "是不是", "能不能", "为啥", "为什么", "怎么", "请问"])


def _save_knowledge(conn, category: str, items: list):
    for content, freq in items:
        conn.execute(
            "INSERT INTO global_knowledge (category, content, freq) VALUES (?,?,?)",
            (category, content, freq),
        )


def get_global_knowledge() -> dict:
    """获取全局学习结果，返回分类好的知识"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT category, content, freq FROM global_knowledge ORDER BY category, freq DESC"
    ).fetchall()
    result = {"slang": [], "expression": [], "pattern": [], "ai_slang": [], "ai_pattern": []}
    for r in rows:
        cat = r["category"]
        if cat in result:
            result[cat].append((r["content"], r["freq"]))
    return result


def get_global_hint() -> str:
    """为 AI 生成全局学习提示（真人的学习，AI的当反面教材）"""
    know = get_global_knowledge()
    parts = []
    if know.get("slang"):
        sample = random.sample(know["slang"], min(6, len(know["slang"])))
        parts.append("真人在聊天时常用的口语/梗：" + "、".join(s for s, _ in sample))
    if know.get("pattern"):
        sample = random.sample(know["pattern"], min(3, len(know["pattern"])))
        parts.append("真人常见的聊天方式：" + "；".join(s for s, _ in sample))
    if know.get("ai_slang"):
        sample = random.sample(know["ai_slang"], min(3, len(know["ai_slang"])))
        parts.append("注意：以下词语听起来很像AI，尽量避免使用：" + "、".join(s for s, _ in sample))
    return " | ".join(parts) if parts else ""
