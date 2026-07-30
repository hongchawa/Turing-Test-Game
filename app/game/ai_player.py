"""
图灵测试 · AI 玩家类
无固定人设 -- 通过镜像学习对方语言习惯来回复
"""
import random
import re
import unicodedata
import uuid
import urllib.parse
from datetime import datetime

import httpx

from app.config import AI_CONFIG
from app.ai.llm import chat_completion, chat_completion_stream, chat_with_tools
from app.ai.expressor import rewrite as expressor_rewrite, rewrite_stream as expressor_rewrite_stream
from app.ai.learner import learn as learn_user_style
from app.db import (
    get_conversation, add_conversation, clear_conversation,
    get_learned_styles, replace_learned_styles,
    get_learned_jargon, replace_learned_jargon,
    search_memory, get_memory_summary,
)
from app.ai.global_learner import get_global_hint
from app.logger import chat_log, think_log, plan_log, express_log, learn_log, reply_log, error_log


FALLBACK_REPLIES = [
    "嗯嗯, 继续说",
    "有道理诶",
    "哈哈, 然后呢",
    "确实",
    "我懂你",
    "展开说说",
    "有意思",
]

# 存储最近 N 条 AI 交互详情 (供管理面板查看)
_PROMPT_DEBUG_LOG: list = []
_MAX_DEBUG_ENTRIES = 30


def get_prompt_debug_log() -> list:
    return list(_PROMPT_DEBUG_LOG)


def _clean_gibberish(text: str) -> str:
    """清除模型生成的全大写英文乱码(  如 MEISHI/MEINFO/YE等幻觉产物  )"""
    cleaned = re.sub(r'\b[A-Z]{3,}\b', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned if cleaned else text

NAME_QUESTION_PATTERNS = re.compile(
    r"你叫(什么|啥)|(什么|啥)名字|你是谁|你(是)?(AI|机器人|程序|人机)|怎么称呼|贵姓",
    re.IGNORECASE,
)


class AiPlayer:
    """AI 玩家 -- 每人设有独立身份 + 独立记忆"""

    def __init__(self):
        from app.ai.personas import AI_PERSONAS
        self.id = f"ai_{uuid.uuid4().hex[:8]}"
        self.persona = random.choice(AI_PERSONAS)
        self.real_name = self.persona["name"]
        self.name = "用户"
        self.avatar = self.persona.get("avatar", "💬")
        # session_id 含人设名, 各人设记忆独立
        self.session_id = f"{self.persona['name']}_{self.id}"
        self.learned_expressions: list = []
        self.learned_jargon: list = []
        self._user_msg_count = 0
        saved = get_conversation(self.session_id)
        self.conversation_history: list = saved or []
        self.message_count = len([r for r, _ in saved if r == "assistant"])
        self.intro_sent = self.message_count > 0
        self.learned_expressions = get_learned_styles(self.session_id)
        self.learned_jargon = get_learned_jargon(self.session_id)

    # ---------- 公共 API ----------
    def _should_use_real_ai(self) -> bool:
        return bool(AI_CONFIG.get("ai_enabled")) and bool(AI_CONFIG.get("api_key"))

    async def generate_response(self, message: str) -> str:
        self.message_count += 1

        if self._should_use_real_ai():
            try:
                # 在线学习: 每收到 2 条对方消息, 归纳一次习惯
                self._user_msg_count += 1
                if self._user_msg_count >= 2 and self._user_msg_count % 2 == 0:
                    user_msgs = [c for r, c in self.conversation_history if r == "user"]
                    user_msgs.append(message)
                    exprs, jargon = await learn_user_style(user_msgs)
                    # 去重: 只存新出现的风格和口头禅, 避免反复学同样的东西
                    if exprs:
                        new_exprs = [e for e in exprs if e not in self.learned_expressions]
                        if new_exprs:
                            self.learned_expressions.extend(new_exprs)
                            replace_learned_styles(self.session_id, self.learned_expressions)
                    if jargon:
                        new_jargon = [j for j in jargon if j not in self.learned_jargon]
                        if new_jargon:
                            self.learned_jargon.extend(new_jargon)
                            replace_learned_jargon(self.session_id, self.learned_jargon)
                    if exprs or jargon:
                        learn_log(
                            [f"当『{s}』时用『{t}』" for s, t in self.learned_expressions[:4]],
                            self.learned_jargon,
                        )
                chat_log(self.name, "[MSG]", message[:50] + ("..." if len(message) > 50 else ""))
                reply = await self._call_llm(message)
                reply_log(self.name, reply)
                return reply
            except Exception as e:
                error_log("Generate", str(e))

        return self._fallback_reply(message)

    async def get_intro_message(self) -> str:
        self.intro_sent = True
        if self._should_use_real_ai():
            try:
                chat_log(self.name, "🎬", "开场…")
                intro = await self._call_llm("", force_intro=True)
                reply_log(self.name, intro)
                return intro
            except Exception as e:
                error_log("Intro", str(e))
        self.message_count += 1
        return self._fallback_reply("")

    # ---------- 核心流水线(  MaiBot 风格: Replyer → Expressor  )----------
    async def _call_llm(self, message: str, force_intro: bool = False) -> str:
        system_parts = []

        # 镜像学习: 只学对方的说话风格, 不预设任何身份
        if self.learned_expressions or self.learned_jargon:
            parts = ["观察到的对方语言习惯, 自然地模仿: "]
            if self.learned_expressions:
                parts.append("表达习惯: " + "；".join(
                    f"当「{s}」时可以「{t}」" for s, t in self.learned_expressions[:4]))
            if self.learned_jargon:
                parts.append("对方偶尔用的词: 、" .join(self.learned_jargon[:6]))
            system_parts.append(" ".join(parts))
        else:
            system_parts.append("像普通网友一样聊天, 观察对方的语言习惯")

        # 回复规则
        system_parts.append(
            "你是普通网友, 没有预设身份。不知道的事情就说不知道。"
            "回复随机5-25字。不要承认自己是AI。不要用emoji除非气氛到了。"
            "被问是不是AI时像普通人一样回应。跳跃接话, 不强行延续话题。"
            "绝对不要编造自己的职业、经历、生活细节。"
            "重要: 只回应对方说的内容。对方说你好你就回你好/嗨/哈喽, 不要主动提天气、地点等无关话题。"
        )

        # 全局学习: 从所有用户的对话中学习常见聊天方式
        global_hint = get_global_hint()
        if global_hint:
            system_parts.append(f"你见过很多人在聊天中这样说话, 可以参考: {global_hint}")

        # 名字检测
        if message and NAME_QUESTION_PATTERNS.search(message):
            system_parts.append("对方在问你的名字。自然地回应, 不要报固定名字, 幽默化解或反问回去")

        system_prompt = "\n".join(system_parts)
        messages = [{"role": "system", "content": system_prompt}]
        for role, content in self.conversation_history:
            messages.append({"role": role, "content": content})

        if force_intro:
            messages.append({"role": "user", "content": "(  游戏刚开始, 主动打个招呼, ≤20字  )"})
        else:
            messages.append({"role": "user", "content": message})

        # 单字符简回
        if message and len(message.strip()) == 1 and not message.strip().isalnum():
            short = random.choice(["？", "...", "嗯？", "咋了"])
            self.conversation_history.append(("user", message))
            self.conversation_history.append(("assistant", short))
            add_conversation(self.session_id, "user", message)
            add_conversation(self.session_id, "assistant", short)
            return short

        # 乱码简回
        if message and _is_keyboard_mash(message):
            short = random.choice(["？", "...", "你键盘坏了？", "？？"])
            self.conversation_history.append(("user", message))
            self.conversation_history.append(("assistant", short))
            add_conversation(self.session_id, "user", message)
            add_conversation(self.session_id, "assistant", short)
            return short

        # ---- Replyer: 直接用系统提示词生成回复 ----
        print("  [THINK] raw: ", end="", flush=True)
        try:
            raw = await chat_completion_stream(messages, on_token=lambda t: print(t, end="", flush=True))
        except Exception:
            raw = await chat_completion(messages)
        print()

        # 记录调试日志
        _PROMPT_DEBUG_LOG.append({
            "time": datetime.now().strftime("%H:%M:%S"),
            "system": system_prompt,
            "user": message,
            "raw": raw,
            "final": "",
        })
        if len(_PROMPT_DEBUG_LOG) > _MAX_DEBUG_ENTRIES:
            _PROMPT_DEBUG_LOG.pop(0)

        if not raw:
            raw = random.choice(self.persona.get("fallback_responses", ["嗯"]))

        # ---- Expressor: 仅做轻量润色, 不改变含义 ----
        raw = _clean_gibberish(raw)
        final = raw
        try:
            expr_prompt = (
                "把下面这句话改写得更口语化, 像随手发的消息。可以加语气词(嗯、哈、哟)或调整顺序, "
                "但不要改变含义。不要加emoji。只输出一句话, ≤30字。"
            )
            expr_msgs = [{"role": "system", "content": expr_prompt}, {"role": "user", "content": raw}]
            print("  [EXPR] expr: ", end="", flush=True)
            polished = await chat_completion_stream(
                expr_msgs, on_token=lambda t: print(t, end="", flush=True), max_tokens_override=60
            )
            print()
            if polished and len(polished) >= 2:
                polished = re.sub(r'[【\[].*?[】\]]\s*', '', polished)
                polished = polished.strip().strip('"\'').strip("\u201c\u201d\u300c\u300d")
                if polished:
                    final = polished
        except Exception:
            pass
        # 把最终回复写回日志
        if _PROMPT_DEBUG_LOG:
            _PROMPT_DEBUG_LOG[-1]["final"] = final

        # 安全网
        final = self._clean_text(final)
        if self._is_garbage(final) or not final:
            final = self._fallback_reply(message)

        # 持久化到数据库
        if force_intro:
            self.conversation_history.append(("assistant", final))
            add_conversation(self.session_id, "assistant", final)
        else:
            self.conversation_history.append(("user", message))
            self.conversation_history.append(("assistant", final))
            add_conversation(self.session_id, "user", message)
            add_conversation(self.session_id, "assistant", final)

        return final

    # ---------- 安全网 ----------
    def _clean_text(self, text: str) -> str:
        out = []
        for ch in text:
            if ch in "\n\t ":
                out.append(ch)
                continue
            o = ord(ch)
            allowed = (
                (0x20 <= o <= 0x7E) or (0x2000 <= o <= 0x206F) or (0x3000 <= o <= 0x303F)
                or (0x3400 <= o <= 0x4DBF) or (0x4E00 <= o <= 0x9FFF) or (0xFF00 <= o <= 0xFFEF)
                or (0x2600 <= o <= 0x27BF) or (0x1F000 <= o <= 0x1FAFF) or (0xFE00 <= o <= 0xFE0F)
            )
            if allowed:
                out.append(ch)
        return "".join(out).strip().replace("\n", " ")

    def _is_garbage(self, text: str) -> bool:
        if not text:
            return True
        if re.search(r"\b(assistant|user|system)\b", text, re.IGNORECASE):
            return True
        weird = sum(1 for ch in text if ch not in "\n\t " and not (
            (0x20 <= ord(ch) <= 0x7E) or (0x4E00 <= ord(ch) <= 0x9FFF)
            or (0x3400 <= ord(ch) <= 0x4DBF) or (0x3000 <= ord(ch) <= 0x303F)
            or (0xFF00 <= ord(ch) <= 0xFFEF) or (0x1F000 <= ord(ch) <= 0x1FAFF)
            or (0x2600 <= ord(ch) <= 0x27BF) or (0xFE00 <= ord(ch) <= 0xFE0F)
        ))
        total = len(text.replace("\n", "").replace("\t", "").replace(" ", ""))
        return total == 0 or weird / total > 0.15

    def _fallback_reply(self, message: str) -> str:
        reply = random.choice(FALLBACK_REPLIES)
        self.conversation_history.append(("user", message)) if message else None
        self.conversation_history.append(("assistant", reply))
        if message:
            add_conversation(self.session_id, "user", message)
        add_conversation(self.session_id, "assistant", reply)
        return reply


# ---- 键盘滚脸检测(  模块级函数  )----
def _is_keyboard_mash(text: str) -> bool:
    """检测是否为无意义乱码/键盘滚脸"""
    t = text.strip()
    if len(t) < 6:  # 太短不判(  hello/hi 之类  )
        return False
    # 纯英文无空格连续字母(  无中文、无标点、无空格  )
    alpha = sum(1 for c in t if c.isascii() and c.isalpha())
    spaces = sum(1 for c in t if c.isspace())
    digits = sum(1 for c in t if c.isdigit())
    others = len(t) - alpha - spaces - digits
    # 超过 60% 字母, 且几乎没有标点/中文, 且字母无规律 = 滚脸
    if alpha / len(t) > 0.6 and others / len(t) < 0.1 and spaces / len(t) < 0.1:
        if len(re.findall(r'[a-zA-Z]+', t)) <= 2:
            return True
    # 全是随机重复字符
    if len(set(t)) / len(t) < 0.3 and len(t) > 4:
        return True
    return False


def _looks_like_question(text: str) -> bool:
    """判断消息是否像知识性问题 (需要搜索)"""
    t = text.strip()
    if len(t) < 4:
        return False
    # 含问号或疑问词 + 足够长度
    question_words = ["吗", "什么", "怎么", "为什么", "谁", "哪", "多少", "几点", "是什么", "什么是", "如何", "啥"]
    has_q = "?" in t or "？" in t or any(w in t for w in question_words)
    if not has_q:
        return False
    # 不是简单寒暄
    simple = ["你好", "在吗", "在干嘛", "吃了吗", "睡了吗", "几点", "几点了"]
    if any(t.startswith(s) and len(t) <= len(s) + 3 for s in simple):
        return False
    return True


# ---- 网页搜索工具(  模块级函数  )----
async def _web_search(query: str, max_results: int = 3) -> str:
    """搜索引擎: 先用 Bing, 失败则降级到 DuckDuckGo (不依赖第三方库)"""
    ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

    # -- 方案 A: Bing --
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as c:
            resp = await c.get(
                f"https://www.bing.com/search?q={urllib.parse.quote(query)}&setlang=zh-Hans",
                headers={"User-Agent": ua},
            )
            resp.raise_for_status()
            text = resp.text
            # 从 HTML 中提取摘要文本
            import re as _re
            # Bing 结果在 <li class="b_algo"> 内部, 取摘要文本
            snippets = []
            for block in _re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', text, _re.DOTALL)[:max_results]:
                # 拿 <p> 内容
                ps = _re.findall(r'<p[^>]*>(.*?)</p>', block, _re.DOTALL)
                # 或者拿 <span class="st"> 内容(  旧版 Bing  )
                spans = _re.findall(r'class="st"[^>]*>(.*?)</(?:span|div)>', block, _re.DOTALL)
                for src in ps + spans:
                    s = _re.sub(r'<[^>]+>', '', src).strip()
                    if s:
                        snippets.append(s)
                        break
            if snippets:
                import html as _html
                clean = [_html.unescape(s) for s in snippets[:max_results]]
                return " | ".join(clean)[:500]
    except Exception:
        pass

    # -- 方案 B: DuckDuckGo HTML(  比 Instant Answer 更全  )--
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as c:
            resp = await c.get(
                f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}",
                headers={"User-Agent": ua},
            )
            resp.raise_for_status()
            text = resp.text
            import re as _re
            # DuckDuckGo 结果在 class="result__snippet" 里
            snippets = _re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>', text, _re.DOTALL)[:max_results]
            if snippets:
                cleaned = [_re.sub(r'<[^>]+>', '', s).strip() for s in snippets]
                return " | ".join(s for s in cleaned if s)[:500]
    except Exception:
        pass

    # -- 方案 C: DuckDuckGo Instant Answer(  旧方案, 全量后备  )--
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            resp = await c.get(
                f"https://api.duckduckgo.com/?q={urllib.parse.quote(query)}&format=json&no_html=1",
                headers={"User-Agent": ua},
            )
            data = resp.json()
            parts = []
            if data.get("AbstractText"):
                parts.append(data["AbstractText"])
            if data.get("Answer"):
                parts.append(data["Answer"])
            for t in data.get("RelatedTopics", [])[:max_results]:
                if t.get("Text"):
                    parts.append(t["Text"].split(" - ")[0])
            if parts:
                return " | ".join(parts[:max_results])[:500]
    except Exception:
        pass

    return "未找到相关信息"


async def _summarize_search(query: str, raw: str) -> str:
    """用 LLM 把搜索结果总结成口语化的几句话(  像朋友告诉你  )"""
    prompt = (
        f"用户搜索了「{query}」, 以下是搜索结果: \n{raw[:600]}\n\n"
        "请用极度口语化、简短的方式(  ≤30字  )告诉我关键信息, 像一个朋友在微信上随口告诉你。"
        "如果搜索结果太学术或和专业术语太多了, 就简单说没找到靠谱的。不要用 emoji, 不要 markdown。"
    )
    try:
        summary = await chat_completion(
            [{"role": "user", "content": prompt}],
            max_tokens_override=80,
        )
        s = summary.strip()
        return s[:80] if s else raw[:40]
    except Exception:
        return raw[:60]
