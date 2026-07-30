"""
多人游戏模式: 玩家大厅 → AI混入 → 群聊 → 讨论 → 投票淘汰
角色: 人类(找AI), 假AI(骗票出), AI(隐藏)
"""
import asyncio
import random
import uuid
import time

from app.logger import chat_log, reply_log

# 全局状态
multi_lobby: list = []          # 等待中的玩家
multi_rooms: dict = {}          # {room_id: MultiRoom}
multi_countdown_task = None     # 倒计时任务
multi_countdown_seconds = 0     # 当前倒计时剩余秒数
MULTI_START_DELAY = 20          # 满 5 人后等 20 秒
DISCUSS_TIME = 180              # 讨论阶段 3 分钟
VOTE_TIME = 30                  # 投票阶段 30 秒
GAME_TOTAL_TIME = 900           # 游戏总时长 15 分钟
MIN_PLAYERS = 5                 # 最少 5 人开局
AI_FILL_DELAY = 30              # 人数不够时，30 秒后自动混入 AI
CHAT_TO_DISCUSS_DELAY = 8       # 所有人发过消息后 8 秒自动进入讨论
CHAT_ABSOLUTE_TIMEOUT = 60      # 聊天阶段绝对超时（秒）


def _notify_multi_watchers(room, sender_name, content, is_ai=False, sticker=None, role=None):
    """通知多人模式的观战者"""
    try:
        from app.game.manager import game_manager
        watcher_ids = game_manager.watchers.get(f"m_{room.id}", set()).copy()
        for wid in watcher_ids:
            player = game_manager.players.get(wid)
            if player and player.websocket:
                data = {
                    "type": "watch_chat",
                    "room_id": f"m_{room.id}",
                    "sender": sender_name,
                    "content": content,
                    "is_ai": is_ai,
                }
                if sticker:
                    data["sticker"] = sticker
                if role:
                    data["role"] = role
                asyncio.ensure_future(player.send_json(data))
    except Exception:
        pass


class MultiPlayer:
    """多人模式玩家"""
    def __init__(self, player):
        self.ref = player           # 原始 Player 对象
        self.num = 0                # 玩家编号 1-N（随机分配）
        self.is_ai = False
        self.role = "human"         # "human" / "fake_ai" / "ai"
        self.real_name = ""         # AI 的真实名字（揭示时才显示）
        self.original_nickname = "" # 真人原始昵称（结算时显示）
        self.ai_system_prompt = ""  # AI 专属 system prompt
        self.ai_history = []        # AI 看到的聊天历史
        self.voted = False
        self.vote_target = None
        self.messages_sent = 0
        self._last_reply_round = 0  # 上次回复的轮次，避免连续回复
        self.spectating = False     # 淘汰后是否在观战


class MultiRoom:
    """多人房间"""
    def __init__(self):
        self.id = uuid.uuid4().hex[:6]
        self.players: list[MultiPlayer] = []
        self.status = "waiting"     # waiting / chatting / discuss / voting / finished
        self.chat_count = 0
        self.chat_log: list[dict] = []  # 观战历史 [{sender, content, is_ai, sender_id}]
        self.discuss_task = None
        self.vote_task = None
        self.game_task = None       # 15 分钟总计时
        self.auto_transition_task = None  # 聊天→讨论自动转换
        self.created_at = time.time()
        self.round_num = 0           # 第几轮
        self.eliminated = set()      # 已被淘汰的玩家 num
        self.ai_found = []           # 已找到的 AI real_name 列表
        self.fake_ai_num = None      # 假AI的编号
        self.vote_history = []       # 每轮投票详情
        self._used_fallback = []     # 已用过的回退词（避免 AI 说一样的话）

    @property
    def real_count(self):
        return sum(1 for p in self.players if not p.is_ai)

    @property
    def ai_count(self):
        return sum(1 for p in self.players if p.is_ai)

    def add_real(self, player_ref):
        mp = MultiPlayer(player_ref)
        mp.original_nickname = player_ref.name
        self.players.append(mp)
        self._renumber()
        return mp

    def add_ai(self, ai_player):
        mp = MultiPlayer(ai_player)
        mp.is_ai = True
        mp.role = "ai"
        mp.ai_system_prompt = self._gen_ai_prompt(ai_player.name)
        self.players.append(mp)
        self._renumber()
        return mp

    def _gen_ai_prompt(self, name):
        return (
            f"你叫{name}，你在一个群聊里聊天。群里有人也有AI，但你不确定谁是谁。"
            "你是一个普通网友。回复要简短随意（5-20字），可以附和别人、吐槽、开玩笑。"
            "绝对不能说'我觉得XX是AI'、'XX说话像AI'、'XX很可疑'这类分析谁是AI的话——"
            "正常人在这种场合不会这样说话。你只能像普通群聊一样聊天。"
            "如果别人提到投票，你可以说'没想好'、'再看看'之类的话。"
            "不要承认自己是AI。不要用emoji。不要过于礼貌。"
            "不要编造职业、经历。保持路人感。"
        )

    def _renumber(self):
        for i, p in enumerate(self.players, 1):
            p.num = i

    def get_player(self, ref):
        for p in self.players:
            if p.ref is ref:
                return p
        return None

    async def broadcast(self, data: dict, exclude=None):
        for p in self.players:
            if p.is_ai or p.ref is exclude:
                continue
            # 观战者只收特定消息类型
            if p.spectating and data.get("type") not in (
                "multi_chat", "multi_sticker", "multi_eliminate",
                "multi_result_all", "multi_vote_results",
                "multi_discuss_start", "multi_vote_start",
                "multi_vote_update", "multi_error",
            ):
                continue
            try:
                await p.ref.send_json(data)
            except Exception:
                pass

    async def tell(self, ref, data: dict):
        for p in self.players:
            if p.ref is ref:
                try:
                    await p.ref.send_json(data)
                except Exception:
                    pass
                return



# ==================== 管理员强开 ====================

async def handle_multi_admin_force(player):
    """管理员跳过等待和倒计时，立即开始游戏（≥1 真人即可）"""
    if not getattr(player, 'is_admin', False):
        await player.send_json({"type": "multi_error", "message": "仅管理员可用"})
        return

    global multi_lobby, multi_countdown_task, multi_countdown_seconds

    # 取消正在进行的倒计时
    if multi_countdown_task and not multi_countdown_task.done():
        multi_countdown_task.cancel()
    multi_countdown_seconds = 0
    multi_countdown_task = None

    # 收集当前大厅所有真人
    all_players = [p for p in multi_lobby if not getattr(p, 'is_ai', False)]
    if not all_players:
        await player.send_json({"type": "multi_error", "message": "大厅没人"})
        return

    # 通知所有人
    for p in all_players:
        try:
            await p.send_json({"type": "multi_count_update", "count": len(all_players)})
        except Exception:
            pass

    print(f"  [MULTI] 管理员 {player.name} 强制开始 ({len(all_players)}人)")

    # 直接创建游戏
    await _create_multi_game(all_players)


# ==================== 加入/离开 ====================

async def handle_multi_join(player, data=None):
    from app.game.manager import Player as RealPlayer
    if not isinstance(player, RealPlayer):
        return
    if data:
        nickname = data.get("nickname", "").strip()
        if nickname:
            player.name = nickname

    for room in multi_rooms.values():
        if any(p.ref is player for p in room.players):
            await player.send_json({"type": "multi_error", "message": "你已在房间中"})
            return

    multi_lobby.append(player)
    total = sum(1 for p in multi_lobby if not getattr(p, 'is_ai', False))

    msg = {"type": "multi_waiting", "count": total}
    if multi_countdown_seconds > 0:
        msg["countdown_seconds"] = multi_countdown_seconds
    await player.send_json(msg)

    for p in multi_lobby:
        if not getattr(p, 'is_ai', False):
            try:
                await p.send_json({"type": "multi_count_update", "count": total})
            except Exception:
                pass

    print(f"  [MULTI] {player.name} 加入大厅 ({total}/{MIN_PLAYERS})")

    if total >= MIN_PLAYERS:
        await _start_countdown()
    else:
        # 人不够 → 30秒后自动混入AI
        _schedule_ai_fill(total)


async def handle_multi_leave(player):
    if player in multi_lobby:
        multi_lobby.remove(player)
        total = sum(1 for p in multi_lobby if not getattr(p, 'is_ai', False))
        for p in multi_lobby:
            if not getattr(p, 'is_ai', False):
                try:
                    await p.send_json({"type": "multi_count_update", "count": total})
                except Exception:
                    pass
        # 人数不足 3 时取消倒计时 + 重新启动填充定时器
        if total < 3 and multi_countdown_task and not multi_countdown_task.done():
            multi_countdown_task.cancel()
            global multi_countdown_seconds
            multi_countdown_seconds = 0
            if total > 0:
                _schedule_ai_fill(total)
            for p in multi_lobby:
                if not getattr(p, 'is_ai', False):
                    try:
                        await p.send_json({"type": "multi_count_update", "count": total})
                    except Exception:
                        pass
            print(f"  [MULTI] 人数不足，取消倒计时（当前 {total} 人）")
        return

    for room_id, room in list(multi_rooms.items()):
        mp = room.get_player(player)
        if mp:
            room.players.remove(mp)
            room._renumber()

            # 通知观战者
            from app.game.manager import game_manager
            game_manager.notify_watchers(f"m_{room_id}", {
                "type": "watch_system",
                "room_id": f"m_{room_id}",
                "message": f"玩家 #{mp.num} {mp.ref.name} 退出了房间",
            })

            # 如果只剩AI → 直接关闭房间
            if room.real_count == 0:
                print(f"  [MULTI] 房间 {room_id} 只剩AI，关闭")
                await _end_game(room)
                return

            # 广播退出消息
            await room.broadcast({
                "type": "multi_player_left",
                "num": mp.num,
                "name": mp.ref.name,
            })

            # 检查是否到达判断输赢条件（如果只剩1个真人 → 直接结束）
            active_real = [p for p in room.players if not p.is_ai and p.num not in room.eliminated]
            remaining_ais = room.ai_count - len(room.ai_found)

            if room.status in ("chatting", "discuss") and room.chat_count >= 2:
                if len(active_real) <= 1 or remaining_ais <= 0:
                    await _end_game(room)
                    return
            elif len(active_real) <= 0:
                await _end_game(room)
                return

            return


# ==================== 倒计时 & 开局 ====================

async def _start_countdown():
    global multi_countdown_task, multi_countdown_seconds, _fill_task
    # 人够了，取消自动填充
    if _fill_task and not _fill_task.done():
        _fill_task.cancel()
        _fill_task = None
    if multi_countdown_task and not multi_countdown_task.done():
        return

    async def countdown():
        global multi_countdown_seconds
        for sec in range(MULTI_START_DELAY, 0, -1):
            multi_countdown_seconds = sec
            real_now = [p for p in multi_lobby if not getattr(p, 'is_ai', False)]
            for p in real_now:
                try:
                    await p.send_json({
                        "type": "multi_countdown",
                        "seconds": sec,
                        "players": len(real_now),
                    })
                except Exception:
                    pass
            await asyncio.sleep(1)
        multi_countdown_seconds = 0
        final_players = [p for p in multi_lobby if not getattr(p, 'is_ai', False)]
        await _create_multi_game(final_players)

    multi_countdown_task = asyncio.create_task(countdown())


# AI 自动填充定时器（避免 1-2 人永远等不到）
_fill_task = None

def _schedule_ai_fill(total: int):
    """人不够时，30 秒后自动混入 AI"""
    global _fill_task
    if total >= MIN_PLAYERS:
        return
    if _fill_task and not _fill_task.done():
        return  # 已经有个定时器在跑

    async def _fill():
        await asyncio.sleep(AI_FILL_DELAY)
        global _fill_task, multi_countdown_task, multi_countdown_seconds
        # 重新统计当前人数
        current = [p for p in multi_lobby if not getattr(p, 'is_ai', False)]
        if len(current) >= MIN_PLAYERS:
            return  # 已经够了
        if not current:
            return  # 没人了
        # 取消倒计时（如果有的话）
        if multi_countdown_task and not multi_countdown_task.done():
            multi_countdown_task.cancel()
        multi_countdown_seconds = 0
        multi_countdown_task = None
        print(f"  [MULTI] 人不够({len(current)}) → 自动混入AI")
        # 通知玩家
        for p in current:
            try:
                await p.send_json({"type": "multi_count_update", "count": len(current)})
            except Exception:
                pass
        await _create_multi_game(current)

    _fill_task = asyncio.create_task(_fill())


async def _create_multi_game(real_players: list):
    global multi_lobby
    room = MultiRoom()

    for p in real_players:
        if p in multi_lobby:
            multi_lobby.remove(p)
        room.add_real(p)

    # AI 数量：5人→2AI, 6→2, 7→3, 8→3, 9→3, 10→4 ...
    ai_count = max(2, (len(real_players) - 1) // 3 + 1)
    room_id = room.id

    # AI 真实名字池（揭秘时才显示）
    ai_real_names = ["阿杰", "小宇", "大熊", "老王", "小明", "土豆", "路人甲",
                     "西瓜", "咸鱼", "喵喵", "老六", "阿强", "老张"]
    random.shuffle(ai_real_names)

    # 统一命名：所有玩家（真人和AI）都叫 玩家XXXX，无法从名字区分
    used_player_nums = set()

    def _gen_player_name():
        n = random.randint(100, 9999)
        while n in used_player_nums:
            n = random.randint(100, 9999)
        used_player_nums.add(n)
        return f"玩家{n}"

    # 真人也改成 玩家XXXX 格式
    for mp in room.players:
        if not mp.is_ai:
            new_name = _gen_player_name()
            mp.ref.name = new_name

    # 创建 AI（也用 玩家XXXX）
    for i in range(ai_count):
        real = ai_real_names[i] if i < len(ai_real_names) else f"网友{random.randint(100,999)}"
        display = _gen_player_name()
        dummy = type('AI', (), {})()
        dummy.id = f"multi_ai_{room_id}_{i}"
        dummy.name = display
        dummy.ip = "0.0.0.0"
        dummy.user_id = ""
        dummy.websocket = None
        dummy.is_ai = True
        dummy.room_id = room_id
        mp = room.add_ai(dummy)
        mp.real_name = real           # 揭秘时用

    # 分配假AI角色（至少3个真人时才有假AI）
    real_humans = [p for p in room.players if not p.is_ai]
    if len(real_humans) >= 3:
        fake_ai = random.choice(real_humans)
        fake_ai.role = "fake_ai"
        room.fake_ai_num = fake_ai.num
        print(f"  [MULTI] 假AI: #{fake_ai.num} {fake_ai.ref.name} (原名: {fake_ai.original_nickname})")

    # 打乱玩家顺序，AI 不再永远是最后一个
    order_before = [(p.num, p.ref.name, p.role) for p in room.players]
    random.shuffle(room.players)
    room._renumber()
    # 更新 fake_ai_num
    for p in room.players:
        if p.role == "fake_ai":
            room.fake_ai_num = p.num
            break
    order_after = [(p.num, p.ref.name, p.role) for p in room.players]
    print(f"  [MULTI] shuffle: {order_before} → {order_after}", flush=True)

    multi_rooms[room_id] = room
    room.status = "chatting"

    # 构建玩家名单（只含编号和显示名）
    player_names = {mp.num: mp.ref.name for mp in room.players}

    await room.broadcast({
        "type": "multi_started",
        "room_id": room_id,
        "total_players": len(room.players),
    })

    # 给每个真人发送角色信息
    for p in room.players:
        if p.is_ai:
            continue
        role_info = {
            "type": "multi_role_info",
            "room_id": room_id,
            "your_num": p.num,
            "total_players": len(room.players),
            "player_names": player_names,
            "role": p.role,  # "human" 或 "fake_ai"
        }
        if p.role == "human":
            fake_exists = room.fake_ai_num is not None
            role_info["identity"] = "人类"
            role_info["objective"] = "找出所有 AI"
            if fake_exists:
                role_info["warning"] = "小心假AI！他们会伪装成AI骗取你的投票。"
            else:
                role_info["warning"] = ""
        elif p.role == "fake_ai":
            role_info["identity"] = "假AI"
            role_info["objective"] = "骗过其他人，让他们把你票出"
            role_info["warning"] = "如果被票出，你就赢了！如果所有真AI都被找到但你没被票出，你就输了。"
        await room.tell(p.ref, role_info)

    print(f"  [MULTI] 房间 {room_id} 开始: {room.real_count}真人 + {room.ai_count}AI · 15分钟")

    # 15分钟总计时
    async def _total_timeout():
        await asyncio.sleep(GAME_TOTAL_TIME)
        if room.status != "finished":
            print(f"  [MULTI] 房间 {room.id} 时间到！")
            await room.broadcast({"type": "multi_error", "message": "⏰ 时间到！"})
            await _end_game(room, time_up=True)

    room.game_task = asyncio.create_task(_total_timeout())

    # 聊天阶段绝对超时：60秒后自动进入讨论
    async def _chat_timeout():
        await asyncio.sleep(CHAT_ABSOLUTE_TIMEOUT)
        if room.status == "chatting" and room.status != "finished":
            room.status = "discuss"
            await _start_discussion(room)

    room.auto_transition_task = asyncio.create_task(_chat_timeout())


# ==================== 聊天 ====================

async def _ai_decide_and_reply(room: MultiRoom, ai: MultiPlayer, trigger_msg: str, voting=False):
    """AI 决定是否回复 → 返回 True（已回复）或 False（跳过）"""
    if room.status == "voting" or room.status == "finished":
        print(f"  [MULTI] #{ai.num} {ai.ref.name} 投票/结束阶段跳过", flush=True)
        return False
    if ai.num in room.eliminated:
        print(f"  [MULTI] #{ai.num} {ai.ref.name} 已淘汰，跳过回复", flush=True)
        return False
    chat_log(ai.ref.name, "📨", f"[多人] {trigger_msg[:40]}")

    try:
        await asyncio.sleep(random.uniform(1.5, 6.0))
    except asyncio.CancelledError:
        return False

    # 获取表情包列表供 AI 选择
    sticker_list_str = ""
    stickers = []
    try:
        from app.db import get_stickers
        stickers = get_stickers()
        if stickers:
            sticker_list_str = "可用表情包：\n" + "\n".join(
                f"- {s['description'] or s['filename']} (文件:{s['filename']})"
                for s in stickers
            ) + "\n你可以选择发送一个表情包来表达情绪。在回复末尾加 STICKER:文件名 就行。不合适的就不加。\n"
    except Exception:
        pass

    reply = None
    used_llm = False
    raw_text = ""
    try:
        from app.ai.llm import chat_completion, chat_completion_stream
        from app.config import AI_CONFIG
        if AI_CONFIG.get("ai_enabled") and AI_CONFIG.get("api_key"):
            used_llm = True
            history_text = "\n".join(
                f"#{mp.num} {mp.ref.name}：{msg}"
                for mp, msg in ai.ai_history[-12:]
            ) if ai.ai_history else ""
            if voting:
                strategies = [
                    f"{sticker_list_str}你是群聊里的{ai.ref.name}。大家正在讨论，你像个普通网友一样参与：可以说没想好、附和别人、或者随便聊聊。绝对不能分析或怀疑别人是AI。一句话（5-15字）。",
                    f"{sticker_list_str}你是{ai.ref.name}，群聊里的普通网友。大家在聊天，你自然地说句话就好。不要分析谁是AI，正常聊天就行。一句话（5-15字）。",
                ]
                prompt = random.choice(strategies)
            else:
                prompt = f"{sticker_list_str}群聊记录：\n{history_text}\n\n刚才有人说：{trigger_msg}\n作为{ai.ref.name}，要不要回？回一句（5-20字）。不想回输出 SKIP。"
            msgs = [
                {"role": "system", "content": ai.ai_system_prompt},
                {"role": "user", "content": prompt},
            ]
            print("  [THINK] raw: ", end="", flush=True)
            try:
                raw_text = await chat_completion_stream(msgs, on_token=lambda t: print(t, end="", flush=True))
            except Exception:
                raw_text = await chat_completion(msgs)
            print()
            result = raw_text.strip()
            if result.upper().startswith("SKIP") or result == "":
                print(f"  ⏭️  [MULTI] #{ai.num} {ai.ref.name} LLM判断：不回复", flush=True)
                return False
            else:
                reply = result[:60].strip()
                print(f"  [MULTI] #{ai.num} {ai.ref.name} LLM决定回复 -> {reply[:20]}", flush=True)
    except Exception as e:
        print(f"  [MULTI AI] LLM 失败: {e}")

    if not reply:
        if voting:
            pool = [
                "我也在猜呢 好难", "大家说得都有道理",
                "信息还不够 再聊聊吧", "难说 我先观望一下",
                "这个确实不好判断", "再看看吧 不急",
                "让我想想", "太纠结了", "我也不知道 再看看",
            ]
        else:
            pool = [
                "哈哈", "确实", "有意思", "嗯嗯", "我也觉得", "对对对",
                "啥玩意儿", "真的假的", "展开说说", "6", "绝了",
            ]
        # 过滤掉已用过的回退词，避免两个 AI 说一样的话
        available = [w for w in pool if w not in room._used_fallback[-5:]]
        if not available:
            available = pool
        reply = random.choice(available)
        room._used_fallback.append(reply)
        print(f"  [MULTI] #{ai.num} {ai.ref.name} 无LLM - 回退词: {reply}", flush=True)

    if not used_llm and random.random() < 0.30:
        print(f"  [MULTI] #{ai.num} {ai.ref.name} 无LLM - 随机跳过（30%）", flush=True)
        return False

    # 解析 LLM 输出的 STICKER:文件名（仅当 LLM 可用时）
    sticker_file = None
    import re as _re
    if used_llm:
        sticker_match = _re.search(r'STICKER\s*:\s*(\S+)', reply)
        if sticker_match:
            sticker_file = sticker_match.group(1).strip()
            reply = _re.sub(r'\s*STICKER\s*:\s*\S+\s*', '', reply).strip()
            print(f"  [MULTI] #{ai.num} {ai.ref.name} AI选了表情包: {sticker_file}", flush=True)

    reply_log(ai.ref.name, reply)

    # 如果 AI 选了表情包且用了 LLM：文本描述 + 贴纸
    final_content = reply
    if sticker_file:
        final_content = f"[表情包:{sticker_file}]"

    await room.broadcast({
        "type": "multi_chat",
        "sender_num": ai.num,
        "sender_name": ai.ref.name,
        "content": final_content,
        "sticker": sticker_file or None,
    })
    # 记录到观战聊天历史
    room.chat_log.append({
        "sender": ai.ref.name,
        "content": final_content if not sticker_file else f"[贴纸:{sticker_file}]",
        "is_ai": True,
        "sender_id": ai.ref.id,
        "role": "ai",
    })
    # 通知观战者
    _notify_multi_watchers(room, ai.ref.name, final_content, is_ai=True, sticker=sticker_file, role="ai")
    ai.messages_sent += 1
    ai._last_reply_round = room.chat_count
    return True


async def _trigger_ai_replies(room: MultiRoom, sender_num: int, content: str, voting=False):
    """真人消息后触发 AI 回复 — 轮流触发，一次最多一个 AI"""
    # 逐个尝试 AI，直到有一个真的回复
    for ai in room.players:
        if not ai.is_ai:
            continue
        if ai.num in room.eliminated:
            continue
        if ai._last_reply_round >= room.chat_count:
            continue
        replied = await _ai_decide_and_reply(room, ai, content, voting)
        if replied:
            return  # 有一个 AI 回复就够了，其他人不抢


async def handle_multi_sticker(player, data: dict):
    """多人模式发送贴纸"""
    filename = (data.get("filename") or "").strip()
    if not filename:
        return
    room = None
    mp = None
    for r in multi_rooms.values():
        mp = r.get_player(player)
        if mp:
            room = r
            break
    if not room or mp is None or room.status == "finished":
        return
    # 已淘汰且不在观战中
    if mp.num in room.eliminated and not mp.spectating:
        return
    if mp.spectating:
        return
    mp.messages_sent += 1
    await room.broadcast({
        "type": "multi_chat",
        "sender_num": mp.num,
        "sender_name": mp.ref.name,
        "content": f"[贴纸] {filename}",
        "sticker": filename,
        "user_id": getattr(mp.ref, 'user_id', ''),
        "client_msg_id": data.get("client_msg_id", ""),
    })
    # 记录贴纸到观战聊天历史
    room.chat_log.append({
        "sender": mp.ref.name,
        "content": f"[贴纸:{filename}]",
        "is_ai": mp.is_ai,
        "sender_id": getattr(mp.ref, 'user_id', ''),
        "role": mp.role,
    })
    # 通知观战者
    _notify_multi_watchers(room, mp.ref.name, "", is_ai=False, sticker=filename, role=mp.role)

    # 获取贴纸描述给 AI
    sticker_desc = "[贴纸]"
    try:
        from app.db import get_sticker_by_filename
        s = get_sticker_by_filename(filename)
        sticker_desc = f"[贴纸: {s['description']}]" if s and s.get("description") else "[贴纸]"
    except Exception:
        pass

    # 触发 AI 可能的回复
    if room.status == "chatting":
        asyncio.create_task(_trigger_ai_replies(room, mp.num, sticker_desc, voting=False))
    elif room.status == "discuss":
        asyncio.create_task(_trigger_ai_replies(room, mp.num, sticker_desc, voting=True))


async def handle_multi_chat(player, data: dict):
    content = (data.get("content") or "").strip()
    if not content or len(content) > 60:
        return

    room = None
    mp = None
    for r in multi_rooms.values():
        mp = r.get_player(player)
        if mp:
            room = r
            break

    if not room or mp is None:
        return
    if room.status not in ("chatting", "discuss"):
        return
    # 已淘汰且不在观战中
    if mp.num in room.eliminated and not mp.spectating:
        return
    if mp.spectating:
        return

    mp.messages_sent += 1
    room.chat_count += 1

    # 真人消息日志
    tag = "[VOTE]" if room.status == "discuss" else "[CHAT]"
    chat_log(mp.ref.name, tag, content)

    # 记录到 AI 的历史中
    for p in room.players:
        if p.is_ai and p.num not in room.eliminated:
            p.ai_history.append((mp, content))

    # 记录到观战聊天历史
    room.chat_log.append({
        "sender": mp.ref.name,
        "content": content,
        "is_ai": False,
        "sender_id": getattr(mp.ref, 'user_id', ''),
        "role": mp.role,
    })

    # 向房间所有人广播（包括发送者，由前端过滤重复）
    await room.broadcast({
        "type": "multi_chat",
        "sender_num": mp.num,
        "sender_name": mp.ref.name,
        "content": content,
        "user_id": getattr(mp.ref, 'user_id', ''),
        "client_msg_id": data.get("client_msg_id", ""),
    })

    # 通知多人模式观战者
    _notify_multi_watchers(room, mp.ref.name, content, is_ai=False, role=mp.role)

    # 触发 AI 可能的回复
    if room.status == "chatting":
        asyncio.create_task(_trigger_ai_replies(room, mp.num, content, voting=False))
    elif room.status == "discuss":
        asyncio.create_task(_trigger_ai_replies(room, mp.num, content, voting=True))

    # 聊天阶段：所有人发过消息后自动进入讨论
    if room.status == "chatting":
        active_real = [p for p in room.players if not p.is_ai and p.num not in room.eliminated and not p.spectating]
        if active_real and all(p.messages_sent >= 1 for p in active_real):
            # 所有人都发过消息 → 启动延迟进入讨论
            if room.auto_transition_task and not room.auto_transition_task.done():
                room.auto_transition_task.cancel()
            async def _delayed_transition():
                await asyncio.sleep(CHAT_TO_DISCUSS_DELAY)
                if room.status == "chatting" and room.status != "finished":
                    room.status = "discuss"
                    await _start_discussion(room)
            room.auto_transition_task = asyncio.create_task(_delayed_transition())


# ==================== 多轮投票循环 ====================

async def _start_discussion(room: MultiRoom):
    """进入讨论阶段"""
    room.round_num += 1
    active_real = [p for p in room.players if not p.is_ai and p.num not in room.eliminated]
    active_count = len(active_real)
    remaining_ais = room.ai_count - len(room.ai_found)

    # 重置投票状态
    for p in room.players:
        p.voted = False
        p.vote_target = None

    # 如果有上一轮投票结果，展示给玩家
    vote_results_text = ""
    if room.vote_history:
        last_vote = room.vote_history[-1]
        vote_results_text = f"第{last_vote['round']}轮投票结果：\n"
        # 统计谁投了谁
        votes_cast = last_vote.get("votes", {})
        voter_names = {p.num: p.ref.name for p in room.players}
        for voter_num, target_num in votes_cast.items():
            voter_name = voter_names.get(voter_num, f"#{voter_num}")
            target_name = voter_names.get(target_num, f"#{target_num}")
            vote_results_text += f"  {voter_name} → {target_name}\n"
        elim_name = voter_names.get(last_vote.get("eliminated_num"), "?")
        vote_results_text += f"  → 淘汰: {elim_name}"

    discuss_data = {
        "type": "multi_discuss_start",
        "seconds": DISCUSS_TIME,
        "round": room.round_num,
        "remaining_ais": remaining_ais,
    }
    if vote_results_text:
        discuss_data["vote_results"] = vote_results_text

    await room.broadcast(discuss_data)
    print(f"  [MULTI] 房间 {room.id} 第{room.round_num}轮讨论 ({DISCUSS_TIME}s) · 还剩{remaining_ais}个AI")

    # AI 主动插话
    async def _ai_discuss():
        for ai in room.players:
            if not ai.is_ai or ai.num in room.eliminated:
                continue
            await asyncio.sleep(random.uniform(5.0, 15.0))
            if room.status != "discuss":
                return
            pool = [
                "大家多聊聊呗", "信息还不够 再看看吧",
                "我先观望一下", "都不太确定啊", "让我想想",
                "不好说 再看看", "大家别急 慢慢来",
                "太纠结了", "再看看 还有时间",
            ]
            available = [w for w in pool if w not in room._used_fallback[-5:]]
            if not available:
                available = pool
            msg = random.choice(available)
            room._used_fallback.append(msg)
            chat_log(ai.ref.name, "[CHAT]", f"[讨论] {msg}")
            await asyncio.sleep(random.uniform(0.8, 1.8))
            await room.broadcast({
                "type": "multi_chat",
                "sender_num": ai.num,
                "sender_name": ai.ref.name,
                "content": msg,
            })
            # 记录到观战聊天历史
            room.chat_log.append({
                "sender": ai.ref.name,
                "content": msg,
                "is_ai": True,
                "sender_id": ai.ref.id,
                "role": "ai",
            })
            ai.messages_sent += 1

    asyncio.create_task(_ai_discuss())

    async def _transition():
        await asyncio.sleep(DISCUSS_TIME)
        if room.status == "discuss":
            room.status = "voting"
            await _start_voting_phase(room)

    room.discuss_task = asyncio.create_task(_transition())


async def _start_voting_phase(room: MultiRoom):
    """进入投票阶段"""
    all_alive = [p for p in room.players if p.num not in room.eliminated]
    remaining_ais = room.ai_count - len(room.ai_found)
    player_names = {}
    for p in room.players:
        player_names[p.num] = p.ref.name

    await room.broadcast({
        "type": "multi_vote_start",
        "player_count": len(room.players),
        "human_count": len(all_alive),
        "player_names": player_names,
        "timeout": VOTE_TIME,
        "round": room.round_num,
        "remaining_ais": remaining_ais,
    })
    print(f"  [MULTI] 房间 {room.id} 第{room.round_num}轮投票 ({VOTE_TIME}s)")

    # AI 投票
    async def _ai_vote():
        for ai in room.players:
            if not ai.is_ai or ai.num in room.eliminated:
                continue
            # 随机等待 2-6 秒（投票阶段只有 30 秒，不能等太久）
            await asyncio.sleep(random.uniform(2.0, 6.0))
            # 每次醒来都检查状态
            if room.status != "voting" or room.status == "finished":
                return
            if ai.num in room.eliminated:
                continue
            targets = [p for p in room.players if not p.is_ai and p.num != ai.num and p.num not in room.eliminated]
            if targets:
                target = random.choice(targets)
                ai.voted = True
                ai.vote_target = target.num
                # 广播投票进度（AI 投票也计入显示）
                all_alive = [p for p in room.players if p.num not in room.eliminated]
                ai_voted_count = sum(1 for p in all_alive if p.voted)
                await room.broadcast({
                    "type": "multi_vote_update",
                    "voted_count": ai_voted_count,
                    "total_voters": len(all_alive),
                })

    asyncio.create_task(_ai_vote())

    async def _timeout():
        await asyncio.sleep(VOTE_TIME)
        if room.status == "voting":
            await _process_vote(room, timed_out=True)

    room.vote_task = asyncio.create_task(_timeout())


async def handle_multi_vote(player, data: dict):
    target_num = data.get("target")
    if target_num is None:
        return
    target_num = int(target_num)

    room = None
    voter = None
    for r in multi_rooms.values():
        voter = r.get_player(player)
        if voter:
            room = r
            break

    if not room or not voter or voter.voted or room.status != "voting":
        return
    if voter.num in room.eliminated:
        return

    voter.voted = True
    voter.vote_target = target_num

    active_real = [p for p in room.players if not p.is_ai and p.num not in room.eliminated]
    # 进度显示：所有人（含AI）都算
    all_active = [p for p in room.players if p.num not in room.eliminated]
    voted_count = sum(1 for p in all_active if p.voted)
    total_all = len(all_active)

    # 直接发投票结果给投票者（含最新计数）
    await player.send_json({
        "type": "multi_vote_ok",
        "your_vote": target_num,
        "voted_count": voted_count,
        "total_voters": total_all,
    })

    # 广播给其他人
    await room.broadcast({
        "type": "multi_vote_update",
        "voted_count": voted_count,
        "total_voters": total_all,
    }, exclude=player)

    print(f"  [MULTI] {voter.ref.name} 投了 #{target_num} ({voted_count}/{total_all})")

    if all(p.voted for p in active_real) and room.status != "finished":
        await _process_vote(room)


async def _process_vote(room: MultiRoom, timed_out=False):
    """处理投票结果：淘汰得票最多的玩家，判断是否继续"""
    if room.status == "finished":
        return
    if room.status == "voting":
        room.status = "processing"  # 防止重复调用

    for t in (room.discuss_task, room.vote_task):
        if t and not t.done():
            try:
                t.cancel()
            except Exception:
                pass

    # 统计真人投票
    votes = {}
    for p in room.players:
        if not p.is_ai and p.voted and p.vote_target and p.num not in room.eliminated:
            votes[p.num] = p.vote_target  # 记录谁投了谁

    if not votes:
        # 没人投票 → 直接重新讨论
        room.status = "discuss"
        await _start_discussion(room)
        return

    # 找出得票最多的目标
    vote_counts = {}
    for voter_num, target_num in votes.items():
        vote_counts[target_num] = vote_counts.get(target_num, 0) + 1

    max_cnt = max(vote_counts.values())
    top_suspects = [n for n, c in vote_counts.items() if c == max_cnt]
    # 平票时随机选一个
    eliminated_num = random.choice(top_suspects)

    # 记录本轮投票历史
    room.vote_history.append({
        "round": room.round_num,
        "votes": dict(votes),  # {voter_num: target_num}
        "vote_counts": vote_counts,  # {target_num: count}
        "eliminated_num": eliminated_num,
    })

    room.eliminated.add(eliminated_num)
    eliminated_player = next((p for p in room.players if p.num == eliminated_num), None)
    is_ai = eliminated_player.is_ai if eliminated_player else False
    is_fake_ai = eliminated_player.role == "fake_ai" if eliminated_player else False
    eliminated_name = eliminated_player.ref.name if eliminated_player else "?"

    real_name = ""
    if is_ai and eliminated_player:
        real_name = eliminated_player.real_name or eliminated_player.ref.name
        room.ai_found.append(real_name)

    remaining_ais = room.ai_count - len(room.ai_found)
    active_real = [p for p in room.players if not p.is_ai and p.num not in room.eliminated]

    print(f"  [MULTI] 淘汰 #{eliminated_num} {eliminated_name} {'AI!' if is_ai else ('假AI!' if is_fake_ai else '真人')} · 还剩{remaining_ais}个AI")

    # 构建投票详情（谁投了谁）
    voter_names = {p.num: p.ref.name for p in room.players}
    vote_detail = []
    for voter_num, target_num in votes.items():
        vote_detail.append({
            "voter_num": voter_num,
            "voter_name": voter_names.get(voter_num, f"#{voter_num}"),
            "target_num": target_num,
            "target_name": voter_names.get(target_num, f"#{target_num}"),
        })

    # 广播淘汰结果
    eliminate_data = {
        "type": "multi_eliminate",
        "eliminated_num": eliminated_num,
        "eliminated_name": eliminated_name,
        "is_ai": is_ai,
        "is_fake_ai": is_fake_ai,
        "real_name": real_name,
        "remaining_ais": remaining_ais,
        "ai_found_so_far": room.ai_found,
        "round": room.round_num,
        "eliminated_count": len(room.eliminated),
        "time_left": round(GAME_TOTAL_TIME - (time.time() - room.created_at)),
        "vote_detail": vote_detail,
        "vote_counts": vote_counts,
    }
    await room.broadcast(eliminate_data)

    # 通知观战者
    from app.game.manager import game_manager
    game_manager.notify_watchers(f"m_{room.id}", {
        "type": "watch_system",
        "room_id": f"m_{room.id}",
        "message": f"淘汰 #{eliminated_num} {eliminated_name} ({'AI' if is_ai else ('假AI' if is_fake_ai else '真人')})",
    })

    # === 判断游戏是否结束 ===
    # 1. 假AI被票出 → 假AI获胜
    if is_fake_ai:
        await room.broadcast({"type": "multi_error", "message": f"假AI #{eliminated_num} {eliminated_name} 被票出了！假AI获胜！"})
        await _end_game(room, fake_ai_win=True)
        return

    # 2. 所有真AI找到了
    if remaining_ais <= 0:
        # 检查假AI是否还在
        fake_ai_alive = any(p.role == "fake_ai" and p.num not in room.eliminated for p in room.players)
        if fake_ai_alive:
            # 假AI还在，继续游戏直到找出假AI
            active_humans = [p for p in room.players if not p.is_ai and p.num not in room.eliminated]
            if len(active_humans) <= 2:
                # 只剩2人（其中一个是假AI）→ 假AI获胜
                await room.broadcast({"type": "multi_error", "message": "所有AI已找到，但假AI仍在！假AI获胜！"})
                await _end_game(room, fake_ai_win=True)
                return
            # 还有3+人，继续游戏找出假AI
            await room.broadcast({"type": "multi_chat", "sender_num": 0, "sender_name": "系统", "content": "所有真AI已被找出！但假AI还在，继续找出假AI！"})
            await asyncio.sleep(3)
            if room.status != "finished":
                room.status = "discuss"
                await _start_discussion(room)
            return
        else:
            # 没有假AI → 人类胜利
            await room.broadcast({"type": "multi_error", "message": "所有AI都被找到了！人类胜利！"})
            await _end_game(room)
            return

    # 3. 真人都被淘汰了
    if len(active_real) <= 0:
        await room.broadcast({"type": "multi_error", "message": "只剩下AI了…"})
        await _end_game(room)
        return

    # 4. 只剩1个真人 + 假AI → 假AI获胜
    human_alive = [p for p in room.players if not p.is_ai and p.num not in room.eliminated]
    fake_ai_alive = any(p.role == "fake_ai" and p.num not in room.eliminated for p in room.players)
    if len(human_alive) <= 1 and fake_ai_alive:
        await room.broadcast({"type": "multi_error", "message": "只剩一个人类和假AI了！假AI获胜！"})
        await _end_game(room, fake_ai_win=True)
        return
    if len(human_alive) <= 0:
        await room.broadcast({"type": "multi_error", "message": "只剩下AI了…"})
        await _end_game(room)
        return

    # 被淘汰的AI → 踢出房间，不再发言
    if is_ai and eliminated_player:
        eliminated_player.spectating = False  # AI不观战，直接移除
        room.players = [p for p in room.players if p.num != eliminated_num]
        room._renumber()
        # 更新 fake_ai_num
        for p in room.players:
            if p.role == "fake_ai":
                room.fake_ai_num = p.num
                break

    # 被淘汰的真人 → 通知可以选择观战或退出
    elif eliminated_player and not is_ai:
        eliminated_player.spectating = True
        await room.tell(eliminated_player.ref, {
            "type": "multi_eliminated_options",
            "message": "你已被淘汰，可以选择观战或退出",
        })

    # 继续下一轮
    await asyncio.sleep(3)
    if room.status != "finished":
        room.status = "discuss"
        await _start_discussion(room)


async def _end_game(room: MultiRoom, time_up=False, fake_ai_win=False):
    """游戏结束"""
    if room.status == "finished":
        return
    room.status = "finished"

    # 取消所有定时器
    for t in (room.discuss_task, room.vote_task, room.game_task, room.auto_transition_task):
        if t and not t.done():
            try:
                t.cancel()
            except Exception:
                pass

    remaining_ais = room.ai_count - len(room.ai_found)
    # 所有AI在游戏中
    all_ai = [p for p in room.players if p.is_ai]
    ai_nums = [p.num for p in all_ai]
    ai_names = [p.real_name or p.ref.name for p in all_ai]

    # 构建所有玩家信息（含原始昵称和角色）
    all_players_info = []
    for p in room.players:
        all_players_info.append({
            "num": p.num,
            "display_name": p.ref.name,
            "original_nickname": p.original_nickname,
            "role": p.role,
            "is_ai": p.is_ai,
            "eliminated": p.num in room.eliminated,
        })

    # 也加上已被踢出的AI
    for elim_num in room.eliminated:
        if not any(p["num"] == elim_num for p in all_players_info):
            # 找原始信息（从vote_history反推）
            all_players_info.append({
                "num": elim_num,
                "display_name": f"#{elim_num}",
                "original_nickname": "",
                "role": "ai",
                "is_ai": True,
                "eliminated": True,
            })

    if time_up:
        verdict = "⏰ 时间到！AI 胜利"
    elif fake_ai_win:
        verdict = "🎭 假AI获胜！"
    elif remaining_ais <= 0:
        verdict = "人类胜利！找到了所有 AI"
    else:
        verdict = "AI 逃脱！还剩 AI"

    result = {
        "type": "multi_result_all",
        "ai_nums": ai_nums,
        "ai_names": ai_names,
        "verdict": verdict,
        "ai_found": room.ai_found,
        "remaining_ais": remaining_ais,
        "round": room.round_num,
        "player_names": {p.num: p.ref.name for p in room.players},
        "eliminated": list(room.eliminated),
        "time_up": time_up,
        "fake_ai_win": fake_ai_win,
        "all_players": all_players_info,
        "vote_history": room.vote_history,
    }
    for p in room.players:
        if p.is_ai:
            continue
        try:
            await p.ref.send_json(result)
        except Exception:
            pass

    print(f"  [MULTI] 房间 {room.id} 结束: {verdict}")

    async def cleanup():
        await asyncio.sleep(300)
        if room.id in multi_rooms:
            del multi_rooms[room.id]
    asyncio.create_task(cleanup())


async def handle_multi_spectate(player):
    """被淘汰的真人选择继续观战"""
    for room in multi_rooms.values():
        mp = room.get_player(player)
        if mp and mp.num in room.eliminated:
            mp.spectating = True
            await player.send_json({"type": "multi_spectate_ok", "message": "已进入观战模式"})
            # 发送当前聊天历史
            for log_entry in room.chat_log[-50:]:
                await player.send_json({
                    "type": "multi_chat",
                    "sender_num": 0,
                    "sender_name": log_entry["sender"],
                    "content": log_entry["content"],
                    "is_system": True,
                })
            return
    await player.send_json({"type": "multi_error", "message": "无法进入观战"})
