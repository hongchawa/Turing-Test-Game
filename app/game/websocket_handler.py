"""
图灵测试 · WebSocket 消息处理
"""
import asyncio
import json
import random
import time
import re
from datetime import datetime, timedelta

from app.game.multiplayer import (
    handle_multi_join, handle_multi_leave, handle_multi_chat, handle_multi_sticker,
    handle_multi_vote, handle_multi_admin_force,
)

from fastapi import WebSocket, WebSocketDisconnect

from app.config import GAME_CONFIG
from app.game.manager import Player, Room, game_manager


async def handle_player(websocket: WebSocket, player: Player):
    """处理玩家 WebSocket 消息的主循环"""
    game_manager.register_player(player)
    # 新连接玩家收到当前公告
    try:
        from app.main import _current_announce
        if _current_announce:
            await player.send_json({"type": "announce", "content": _current_announce})
    except Exception:
        pass
    # 定时发送应用层 pong 防止客户端因空闲断开
    async def _keepalive_loop():
        while True:
            try:
                await asyncio.sleep(30)
                await player.send_json({"type": "pong", "t": 0})
            except Exception:
                break
    try:
        _keepalive_task = asyncio.create_task(_keepalive_loop())
    except Exception:
        _keepalive_task = None
    from app.db import is_banned, get_ban_info
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # 重连检查：玩家不在房间中，但有 user_id，检查是否有断连等待重连的游戏
            if not player.room_id and not player.is_ai:
                uid = data.get("user_id", "").strip()
                if uid and not player.user_id:
                    player.user_id = uid
                if uid:
                    reconnected = game_manager.try_reconnect(uid, player)
                    if reconnected:
                        room = game_manager.rooms.get(player.room_id)
                        if room:
                            opponent = room.get_opponent(player)
                            await player.send_json({
                                "type": "reconnected",
                                "room_id": room.id,
                                "opponent_name": opponent.name if opponent else "?",
                                "opponent_is_ai": opponent.is_ai if opponent else False,
                                "your_messages_sent": player.messages_sent,
                                "opponent_messages_sent": opponent.messages_sent if opponent else 0,
                            })
                            game_manager.notify_watchers(player.room_id, {
                                "type": "watch_system",
                                "room_id": player.room_id,
                                "message": f"{player.name} 已重新连接",
                            })
                            print(f"  [WS] 玩家 {player.name} 重连成功，恢复房间 {room.id}")
                            continue  # 重新处理这条消息（现在已在房间中）

            # 仅游戏中才检查封禁（主界面由 join_queue 自行检查，避免主循环 break 导致无响应）
            if player.room_id and is_banned(name=player.name, ip=player.ip, user_id=player.user_id, fingerprint=player.fingerprint):
                ban = get_ban_info(name=player.name, ip=player.ip, user_id=player.user_id, fingerprint=player.fingerprint) or {}
                reason = ban.get("reason", "违规行为")
                expires = ban.get("expires_at", "")
                exp_str = "封禁至 " + expires if expires else "永久封禁"
                await player.send_json({
                    "type": "error",
                    "message": f"你已被封禁",
                    "ban_user_id": player.user_id,
                    "ban_reason": reason,
                    "ban_expires": exp_str,
                })
                room = game_manager.rooms.get(player.room_id)
                if room:
                    opponent = room.get_opponent(player)
                    if opponent:
                        await opponent.send_json({
                            "type": "opponent_disconnected",
                            "reason": "对方离开了游戏",
                        })
                    room.finished = True
                    game_manager.cleanup_room(room.id)
                print(f"  [BAN] 游戏中强制踢出 {player.name} (ip={player.ip})")
                break

            msg_type = data.get("type")
            if msg_type == "join_queue":
                await _handle_join_queue(player, data)
            elif msg_type == "leave_queue":
                await _handle_leave_queue(player)
            elif msg_type == "chat_message":
                await _handle_chat_message(player, data)
            elif msg_type == "chat_sticker":
                await _handle_chat_sticker(player, data)
            elif msg_type == "make_judgment":
                await _handle_judgment(player, data)
            elif msg_type == "return_home":
                await _handle_return_home(player)
            elif msg_type == "submit_report":
                await _handle_submit_report(player, data)
            elif msg_type == "admin_login":
                await _handle_admin_login(player, data)
            elif msg_type == "admin_ban":
                await _handle_admin_ban(player, data)
            elif msg_type == "msg_timeout":
                await _handle_msg_timeout(player)
            elif msg_type == "multi_join":
                await _handle_multi_join(player, data)
            elif msg_type == "multi_leave":
                await _handle_multi_leave(player)
            elif msg_type == "multi_chat":
                await _handle_multi_chat(player, data)
            elif msg_type == "multi_sticker":
                await _handle_multi_sticker(player, data)
            elif msg_type == "multi_vote":
                await _handle_multi_vote(player, data)
            elif msg_type == "multi_admin_force":
                await _handle_multi_admin_force(player)
            elif msg_type == "multi_spectate":
                from app.game.multiplayer import handle_multi_spectate
                await handle_multi_spectate(player)
            elif msg_type == "ping":
                await player.send_json({"type": "pong", "t": data.get("t", 0)})
            elif msg_type == "watch_list":
                await _handle_watch_list(player, data)
            elif msg_type == "watch_start":
                await _handle_watch_start(player, data)
            elif msg_type == "watch_stop":
                await _handle_watch_stop(player)
            elif msg_type == "watch_ban":
                await _handle_watch_ban(player, data)
            elif msg_type == "watch_warn":
                await _handle_watch_warn(player, data)
            elif msg_type == "recovery_login":
                await _handle_recovery_login(player, data)
            elif msg_type == "get_recovery_code":
                await _handle_get_recovery_code(player, data)
            elif msg_type == "change_nickname":
                await _handle_change_nickname(player, data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        if "accept" not in str(e).lower():
            print(f"[WS] 错误 player={player.id}: {e}")
    finally:
        if _keepalive_task:
            try: _keepalive_task.cancel()
            except Exception: pass
        # 管理员断线时清除所有观战
        if player.is_admin:
            game_manager.remove_all_watchers_for(player.id)
        # 多人游戏：断线时自动离开大厅/房间
        try:
            from app.game.multiplayer import handle_multi_leave
            await handle_multi_leave(player)
        except Exception:
            pass
        game_manager.unregister_player(player.id)


async def _handle_join_queue(player: Player, data: dict = None):
    # 自动清理旧房间（防上次游戏残留）
    if player.room_id:
        room = game_manager.rooms.get(player.room_id)
        if room:
            room.finished = True
            game_manager.cleanup_room(room.id)
    player.judgment_made = False
    player.judgment_choice = None
    player.messages_sent = 0

    # 读取昵称（前端传的或默认）
    nickname = (data or {}).get("nickname", "").strip()
    player.name = nickname or f"玩家{random.randint(1000, 9999)}"

    # 读取浏览器持久 user_id
    player.user_id = (data or {}).get("user_id", "").strip()

    # 在线用户追踪：如果用户有昵称且有 user_id，自动注册/更新昵称记录
    if player.name and player.user_id:
        try:
            from app.db import register_nickname as _reg_nick
            _reg_nick(player.name, player.user_id, player.ip)
            # 生成恢复码（只有第一次会生成）
            from app.db import generate_recovery_code as _gen_code
            code = _gen_code(player.user_id, player.name, player.ip)
            # 发送恢复码给用户
            asyncio.ensure_future(player.send_json({
                "type": "recovery_code",
                "code": code,
                "user_id": player.user_id,
                "nickname": player.name,
            }))
        except Exception:
            pass

    # IP 注册检查：一个 IP 只能注册一个账号
    try:
        from app.db import check_ip_registered as _check_ip
        if player.ip and player.user_id:
            _ip_uid = _check_ip(player.ip)
            if _ip_uid and _ip_uid != player.user_id:
                await player.send_json({
                    "type": "error",
                    "message": f"该网络环境已注册过账号，请使用恢复码登录",
                    "need_recovery": True,
                    "registered_user_id": _ip_uid,
                })
                return
    except Exception:
        pass

    # 四重封禁检查：name / IP / user_id / fingerprint
    from app.db import is_banned, get_ban_info
    if is_banned(name=player.name, ip=player.ip, user_id=player.user_id, fingerprint=player.fingerprint):
        ban = get_ban_info(name=player.name, ip=player.ip, user_id=player.user_id, fingerprint=player.fingerprint) or {}
        reason = ban.get("reason", "违规行为")
        expires = ban.get("expires_at", "")
        exp_str = "封禁至 " + expires if expires else "永久封禁"
        await player.send_json({
            "type": "error",
            "message": "账号异常",
            "ban_user_id": player.user_id,
            "ban_reason": reason,
            "ban_expires": exp_str,
        })
        return

    # 清理队列中已断开的玩家
    game_manager.waiting_queue = [
        p for p in game_manager.waiting_queue
        if p.id != player.id and p.websocket is not None
    ]

    # AI 概率决定：是否直接分配 AI（超管可配置 ai_probability 0~1）
    ai_prob = float(GAME_CONFIG.get("ai_probability", 0.35))
    if random.random() < ai_prob:
        # 按概率直接分配 AI
        room = game_manager.match_player_with_ai(player)
        ai = room.ai_instance
        await room.start_total_timer()
        await _send_matched(player, room, opponent=None)
        if ai:
            ai.is_ai = True
        await player.send_json({
            "type": "matched", "room_id": room.id,
            "opponent_name": "用户", "is_ai": False,
        })
        async def _send_intro():
            try:
                await asyncio.sleep(random.uniform(4.0, 7.0))
                from app.db import get_intro_templates
                templates = get_intro_templates(10)
                msg = random.choice(templates) if templates else "你好"
                await player.send_json({"type": "chat_message", "sender": "用户", "content": msg})
                game_manager.notify_watchers(room.id, {
                    "type": "watch_chat", "room_id": room.id,
                    "sender": "用户", "content": msg, "is_ai": True,
                })
            except asyncio.CancelledError:
                pass
        room._reply_task = asyncio.create_task(_send_intro())
        return

    # 尝试立即匹配真人对手
    if game_manager.waiting_queue:
        opponent = game_manager.waiting_queue.pop(0)
        room = game_manager.match_players(player, opponent)
        await room.start_total_timer()
        await _send_matched(player, room, opponent)
        await _send_matched(opponent, room, player)
        return

    # 没有等待中的真人 → 加入队列，等 15 秒
    game_manager.waiting_queue.append(player)
    await player.send_json({"type": "waiting", "message": "正在寻找真人对手，最长等待15秒..."})

    async def _wait_and_match():
        """等待 15 秒，有真人就匹配真人，没有则分配 AI"""
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            return
        # 检查队列中是否还有自己（可能已被匹配）
        if player not in game_manager.waiting_queue:
            return
        game_manager.waiting_queue.remove(player)
        # 再检查一次有没有真人加入
        if game_manager.waiting_queue:
            opponent = game_manager.waiting_queue.pop(0)
            room = game_manager.match_players(player, opponent)
            await room.start_total_timer()
            await _send_matched(player, room, opponent)
            await _send_matched(opponent, room, player)
        else:
            # 超时，分配 AI
            room = game_manager.match_player_with_ai(player)
            ai = room.ai_instance
            await room.start_total_timer()
            await _send_matched(player, room, opponent=None)
            if ai:
                ai.is_ai = True
            await player.send_json({
                "type": "matched", "room_id": room.id,
                "opponent_name": "用户", "is_ai": False,
            })
            async def _send_intro():
                try:
                    await asyncio.sleep(random.uniform(4.0, 7.0))
                    from app.db import get_intro_templates
                    templates = get_intro_templates(10)
                    msg = random.choice(templates) if templates else "你好"
                    await player.send_json({"type": "chat_message", "sender": "用户", "content": msg})
                    game_manager.notify_watchers(room.id, {
                        "type": "watch_chat", "room_id": room.id,
                        "sender": "用户", "content": msg, "is_ai": True,
                    })
                except asyncio.CancelledError:
                    pass
            room._reply_task = asyncio.create_task(_send_intro())

    player._match_task = asyncio.create_task(_wait_and_match())


async def _handle_leave_queue(player: Player):
    # 取消等待匹配的任务
    if hasattr(player, '_match_task') and player._match_task and not player._match_task.done():
        player._match_task.cancel()
    if player in game_manager.waiting_queue:
        game_manager.waiting_queue.remove(player)
        await player.send_json({"type": "left_queue"})


async def _handle_chat_message(player: Player, data: dict):
    room = game_manager.rooms.get(player.room_id)
    if not room:
        await player.send_json({"type": "error", "message": "你不在游戏中"})
        return
    content = data.get("content", "").strip()
    if not content or len(content) > 60:
        return
    player.messages_sent += 1
    opponent = room.get_opponent(player)

    if opponent.is_ai:
        ai = room.ai_instance
        if not ai:
            return
        # 通知观战者：玩家发了消息
        game_manager.notify_watchers(player.room_id, {
            "type": "watch_chat", "room_id": player.room_id,
            "sender": player.name, "content": content, "is_ai": False,
        })

        # 打断正在跑的生成（开场白或上一条还在思考的回复）
        intro_cancelled = room._reply_task and not room._reply_task.done()
        if intro_cancelled:
            room._reply_task.cancel()
            room._reply_task = None
            # 用户先说了 → 偷这句话
            # print(f"  [IntroSteal] 用户抢先发言(msg#{player.messages_sent}): {content[:20]}")
            if player.messages_sent == 1 and len(content) >= 2 and len(content) <= 30:
                asyncio.create_task(_steal_intro(content))

        # 用 task 包装回复生成，可被后续消息打断
        my_task = None

        async def _gen_reply():
            nonlocal my_task
            my_task = asyncio.current_task()
            try:
                await asyncio.sleep(random.uniform(0.3, 1.0))
                if room._reply_task is not my_task:
                    return  # 已被取消
                response = await ai.generate_response(content)
                if room._reply_task is not my_task:
                    return
                # AI 自主判定对方语言激烈 → 静默举报，不打断游戏
                if room._reply_task is not my_task:
                    return
                await asyncio.sleep(random.uniform(0.3, 0.5))
                await player.send_json({
                    "type": "chat_message", "sender": "用户", "content": response,
                })
                # 通知观战者：AI 回复了
                game_manager.notify_watchers(player.room_id, {
                    "type": "watch_chat", "room_id": player.room_id,
                    "sender": "用户", "content": response, "is_ai": True,
                })
                # AI 决定是否发表情包（LLM 语义匹配）
                await _maybe_send_sticker(player, response)
                if room._reply_task is not my_task:
                    return
                await room.send_to(player, {"type": "can_judge"})
                # AI 主动判断
                if player.messages_sent >= 5 and not room.ai_has_judged:
                    total = player.messages_sent + (ai.message_count if ai else 0)
                    if total >= 10 and random.random() < 0.4 and not room.finished:
                        await _ai_judge(player, room, ai)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[WS] AI 回复异常: {e}")
                try:
                    await player.send_json({"type": "chat_message", "sender": "用户", "content": "嗯..."})
                except Exception:
                    pass

        room._reply_task = asyncio.create_task(_gen_reply())
        my_task = room._reply_task
        return  # reply 在 task 里发送，主流程直接返回
    else:
        room.conversation_log.append(("user", content))
        await room.send_to(opponent, {
            "type": "chat_message", "sender": "用户", "content": content,
        })
        # 通知观战者：真人发了消息
        game_manager.notify_watchers(player.room_id, {
            "type": "watch_chat", "room_id": player.room_id,
            "sender": player.name, "content": content, "is_ai": False,
        })

    both_ready = player.messages_sent >= 1 and (opponent.messages_sent >= 1 or opponent.is_ai)
    if both_ready:
        await room.send_to(player, {"type": "can_judge"})
        if not opponent.is_ai:
            await room.send_to(opponent, {"type": "can_judge"})


async def _ai_judge(player: Player, room: Room, ai: "AiPlayer"):
    """AI 主动判断用户"""
    room.ai_has_judged = True
    reason = "直觉判断"
    guess = "human"  # 默认真人，LLM 判断后可能改
    try:
        from app.ai.llm import chat_completion
        history_text = "\n".join(
            f"{'对方' if r=='user' else '我'}：{c[:60]}"
            for r, c in ai.conversation_history[-8:]
        )
        prompt = (
            f"以下是最近几轮聊天记录：\n{history_text}\n\n"
            "请仔细判断对方更像真人还是AI。参考标准：\n"
            "- 真人：回复随意、可能有错别字、语气自然、会反问、偶尔答非所问\n"
            "- AI：回复完美无缺、逻辑严密、过于礼貌、从不犯错\n"
            "用1句话（≤20字）给出理由。格式：[真人/AI] 理由"
        )
        result = await chat_completion(
            [{"role": "user", "content": prompt}], max_tokens_override=50,
        )
        result = result.strip()
        if result.startswith("真人"):
            guess = "human"
        elif result.startswith("AI"):
            guess = "ai"
        for sep in [" ", "理由", "：", "是", "因为"]:
            if sep in result:
                parts = result.split(sep, 1)
                if len(parts) > 1:
                    reason = parts[-1].strip()[:30]
                    break
    except Exception:
        reason = random.choice([
            "回复太机械了", "说话风格不像普通人", "逻辑太完美反而可疑",
            "感觉是个聊天机器人", "回复内容和语气有点奇怪",
        ])

    await asyncio.sleep(random.uniform(1.0, 2.5))
    await player.send_json({
        "type": "chat_message", "sender": "用户",
        "content": random.choice([
            "等等，我想问一下，你到底是不是真人啊？",
            "我有点不太确定，你真的是真人吗？",
            "聊了这么久…你不会是个AI吧？",
        ]),
    })

    if player.judgment_made and not room.finished:
        room.finished = True
        await room.cancel_timer()
        await room.cancel_total_timer()
        await player.send_json({
            "type": "game_result",
            "result": "win" if guess == "ai" else "lose",
            "reason": f"你判断对方是{'AI' if player.judgment_choice=='ai' else '真人'} | "
                      f"对方判断你是{'AI' if guess=='ai' else '真人'}（理由：{reason}）",
            "opponent_was_ai": True, "your_guess": player.judgment_choice,
            "opponent_guess": "ai" if guess == "ai" else "human",
            "opponent_real_name": ai.real_name, "your_real_name": player.name,
        })
        game_manager.cleanup_room(room.id)
    else:
        room.judger = player
        await room.send_to(player, {
            "type": "judgment_required",
            "judger_name": "用户",
            "timeout": GAME_CONFIG["judgment_timeout"],
        })
        await room.start_judgment_timer(player)
        room._ai_guess = guess
        room._ai_reason = reason


async def _handle_chat_sticker(player: Player, data: dict):
    """单人模式贴纸 — 触发 AI 回复"""
    filename = data.get("filename", "").strip()
    if not filename:
        return
    room = game_manager.rooms.get(player.room_id)
    if not room or room.finished:
        return
    player.messages_sent += 1
    opponent = room.get_opponent(player)

    # 转发贴纸到对手（AI）界面
    await opponent.send_json({
        "type": "chat_sticker",
        "filename": filename,
        "from": "human",
    })
    # 通知观战者：贴纸
    game_manager.notify_watchers(player.room_id, {
        "type": "watch_sticker", "room_id": player.room_id,
        "sender": player.name, "filename": filename, "is_ai": False,
    })

    # AI 看到贴纸后也回复
    if opponent.is_ai:
        ai = room.ai_instance
        if not ai:
            return
        intro_cancelled = room._reply_task and not room._reply_task.done()
        if intro_cancelled:
            room._reply_task.cancel()
            room._reply_task = None

        # 查贴纸描述给 AI
        sticker_desc = "对方发了个表情包"
        try:
            from app.db import get_sticker_by_filename
            s = get_sticker_by_filename(filename)
            if s and s.get("description"):
                sticker_desc = f"对方发了个[{s['description']}]表情包"
        except Exception:
            pass

        my_task = None
        async def _gen_sticker_reply():
            nonlocal my_task
            my_task = asyncio.current_task()
            try:
                await asyncio.sleep(random.uniform(0.3, 1.0))
                if room._reply_task is not my_task:
                    return
                response = await ai.generate_response(sticker_desc)
                if room._reply_task is not my_task:
                    return
                await asyncio.sleep(random.uniform(0.3, 0.5))
                await player.send_json({
                    "type": "chat_message", "sender": "用户", "content": response,
                })
                # 通知观战者：AI 回复了
                game_manager.notify_watchers(player.room_id, {
                    "type": "watch_chat", "room_id": player.room_id,
                    "sender": "用户", "content": response, "is_ai": True,
                })
                # LLM 自主决定是否回一个表情包（语义匹配）
                await _maybe_send_sticker(player, response)
                if room._reply_task is not my_task:
                    return
                await room.send_to(player, {"type": "can_judge"})
            except asyncio.CancelledError:
                pass

        room._reply_task = asyncio.create_task(_gen_sticker_reply())


async def _maybe_send_sticker(player: Player, reply_context: str):
    """AI 回复后，用 LLM 自主决定是否发送表情包（完全由 LLM 语义判断）"""
    try:
        from app.db import get_stickers
        stickers = get_stickers()
        if not stickers:
            return
        sticker_list = "\n".join(
            f"- {s['description'] or s['filename']} (文件:{s['filename']})"
            for s in stickers
        )
        sticker_prompt = (
            f"你刚才回复了：{reply_context}\n\n"
            f"以下是可用表情包：\n{sticker_list}\n\n"
            "请根据你刚才的回复内容和情绪，判断是否需要发表情包。"
            "如果需要，选一个最贴合你回复情绪的表情包，只输出文件名（如 a.png）。"
            "如果不需要发表情包，输出「无」（一个字）。"
        )
        from app.ai.llm import chat_completion
        sticker_choice = (await chat_completion(
            [{"role": "user", "content": sticker_prompt}],
            max_tokens_override=50,
        ) or "").strip()
        # 如果 LLM 说不需要，直接返回
        if "无" in sticker_choice or not sticker_choice:
            return
        # 模糊匹配：检查 LLM 输出中是否包含任意表情包文件名
        for s in stickers:
            if s["filename"] in sticker_choice:
                await player.send_json({
                    "type": "chat_sticker",
                    "filename": s["filename"],
                })
                # 通知观战者：AI 发了贴纸
                game_manager.notify_watchers(player.room_id, {
                    "type": "watch_sticker", "room_id": player.room_id,
                    "sender": "用户", "filename": s["filename"], "is_ai": True,
                })
                break
    except Exception:
        pass


async def _handle_judgment(player: Player, data: dict):
    room = game_manager.rooms.get(player.room_id)
    if not room or room.finished:
        return
    opponent = room.get_opponent(player)
    both_sent = player.messages_sent >= 1 and (opponent.messages_sent >= 1 or opponent.is_ai)
    if not both_sent:
        await player.send_json({
            "type": "error", "message": "双方都至少发送一条消息后才能进行判断",
        })
        return
    if player.judgment_made:
        await player.send_json({"type": "error", "message": "你已经做出过判断了"})
        return
    guess = data.get("guess", "")
    if guess not in ("human", "ai"):
        return
    player.judgment_made = True
    player.judgment_choice = guess

    if opponent.is_ai:
        ai = room.ai_instance
        real_name = ai.real_name if ai else "?"
        correct = (guess == "ai")

        # AI 已主动判断过 → 双方互判，合并结果
        if room.ai_has_judged:
            room.finished = True
            await room.cancel_timer()
            await room.cancel_total_timer()
            ai_guess = getattr(room, '_ai_guess', 'ai')
            ai_reason = getattr(room, '_ai_reason', '')


            await room.send_to(player, {
                "type": "game_result",
                "result": "win" if correct else "lose",
                "reason": f"对方真实身份：AI（昵称 {real_name}）。"
                          f"对方判断你是{'AI' if ai_guess=='ai' else '真人'}（理由：{ai_reason}）。"
                          f"你的判断{'✅ 正确' if correct else '❌ 错误'}",
                "opponent_was_ai": True, "your_guess": guess,
                "opponent_real_name": real_name, "your_real_name": player.name,
            })
            game_manager.cleanup_room(room.id)
            return

        # AI 还没判断过，走正常流程
        reason_prefix = " [正确]" if correct else " [错误]"
        await room.send_to(player, {
            "type": "game_result",
            "result": "win" if correct else "lose",
            "reason": f"对方真实身份：AI（昵称 {real_name}）" + reason_prefix,
            "opponent_was_ai": True, "your_guess": guess,
            "opponent_guess": "未判断",
            "opponent_real_name": real_name, "your_real_name": player.name,
        })
        room.finished = True
        await room.cancel_total_timer()
        game_manager.cleanup_room(room.id)
        return

    if room.judger is None:
        room.judger = player
        await room.send_to(opponent, {
            "type": "judgment_required", "judger_name": "用户",
            "timeout": GAME_CONFIG["judgment_timeout"],
        })
        await room.send_to(player, {
            "type": "judgment_waiting", "timeout": GAME_CONFIG["judgment_timeout"],
        })
        await room.start_judgment_timer(player)
    else:
        await room.cancel_timer()
        judger = room.judger
        judger_choice = judger.judgment_choice
        judger_opponent = room.get_opponent(judger)
        judger_correct = (judger_choice == "ai") == judger_opponent.is_ai
        player_correct = (guess == "ai") == opponent.is_ai
        player_opponent = room.get_opponent(player)
        judger_reason = " [正确]" if judger_correct else " [错误]"
        await room.send_to(judger, {
            "type": "game_result",
            "result": "win" if judger_correct else "lose",
            "reason": f"对方（{judger_opponent.name if not judger_opponent.is_ai else '?'}）是真人" + judger_reason,
            "opponent_was_ai": False, "your_guess": judger_choice,
            "opponent_guess": "human" if guess == "human" else "ai",
            "opponent_real_name": judger_opponent.name, "your_real_name": judger.name,
        })
        player_reason = " ✅ 正确！" if player_correct else " ❌ 错误！"
        await room.send_to(player, {
            "type": "game_result",
            "result": "win" if player_correct else "lose",
            "reason": f"对方（{player_opponent.name if not player_opponent.is_ai else '?'}）是真人" + player_reason,
            "opponent_was_ai": False, "your_guess": guess,
            "opponent_guess": "human" if judger_choice == "human" else "ai",
            "opponent_real_name": player_opponent.name, "your_real_name": player.name,
        })
        room.finished = True
        await room.cancel_total_timer()
        if room.conversation_log:
            asyncio.create_task(_learn_from_log(room.conversation_log))
        game_manager.cleanup_room(room.id)


async def _learn_from_log(log: list):
    """真人对话结束后，把对话中学到的风格存到知识库"""
    try:
        from app.ai.learner import learn as do_learn
        user_msgs = [c for r, c in log if r == "user"]
        if len(user_msgs) < 3:
            return
        exprs, jargon = await do_learn(user_msgs)
        if exprs:
            from app.db import replace_learned_styles, run_db
            await run_db(replace_learned_styles, "human_session", exprs)
            print(f"  📝 [知识库] 真人对话总结: {len(exprs)} 条风格")
        if jargon:
            from app.db import replace_learned_jargon, run_db
            await run_db(replace_learned_jargon, "human_session", jargon)
            print(f"  📝 [知识库] 口头禅: {jargon}")
    except Exception as e:
        print(f"  [知识库] 学习失败: {e}")


async def _steal_intro(text: str):
    """AI 判断用户的第一句话是否适合作为开场白，适合就存起来"""
    try:
        from app.ai.llm import chat_completion
        from app.db import add_intro_template
        # print(f"  [IntroSteal] 检查: {text[:30]}")
        result = await chat_completion(
            [{"role": "user", "content": f"这句话适合作为社交软件上的开场白吗？只回复 适合 或 不适合：\n{text[:50]}"}],
            max_tokens_override=5,
        )
        # print(f"  [IntroSteal] 结果: {result.strip()}")
        if "适合" in result and "不适合" not in result:
            from app.db import run_db
            await run_db(add_intro_template, text.strip())
            # print(f"  [IntroSteal] ✅ 已添加开场白: {text[:30]}")
        else:
            pass
    except Exception as e:
        # print(f"  [IntroSteal] ⚠️ 异常: {e}")
        pass

async def _handle_return_home(player: Player):
    # 取消可能还在排队的匹配任务
    if hasattr(player, '_match_task') and player._match_task and not player._match_task.done():
        player._match_task.cancel()
    # 从等待队列中移除
    if player in game_manager.waiting_queue:
        game_manager.waiting_queue.remove(player)
    room = game_manager.rooms.get(player.room_id)
    if room:
        room.finished = True
        game_manager.cleanup_room(room.id)
    player.judgment_made = False
    player.judgment_choice = None
    player.messages_sent = 0
    await player.send_json({"type": "returned_home"})


async def _handle_submit_report(player: Player, data: dict):
    """处理举报（单人/多人通用）"""
    reason = data.get("reason", "").strip()
    if not reason:
        await player.send_json({"type": "error", "message": "请填写举报理由"})
        return
    target_num = data.get("target_num")

    # 多人模式举报
    if target_num:
        from app.game.multiplayer import multi_rooms
        room = None
        reporter_mp = None
        target_mp = None
        for r in multi_rooms.values():
            reporter_mp = r.get_player(player)
            if reporter_mp:
                room = r
                for p in r.players:
                    if p.num == int(target_num):
                        target_mp = p
                        break
                break
        if not room or not target_mp:
            await player.send_json({"type": "error", "message": "找不到举报对象"})
            return
        target_name = target_mp.ref.name
        target_ip = getattr(target_mp.ref, 'ip', '') if not target_mp.is_ai else "AI"
        reporter_ip = getattr(player, 'ip', '')
        # 收集聊天记录
        full_log = "\n".join(
            f"#{mp.num} {mp.ref.name}：{msg}"
            for mp, msg in target_mp.ai_history[-30:]
        ) if target_mp.ai_history else ""
        from app.db import save_report, run_db
        await run_db(save_report,
            room.id, player.name, f"#{target_num} {target_name}", reason, "",
            full_log, reporter_ip=reporter_ip, offender_ip=target_ip,
        )
        await player.send_json({"type": "report_submitted", "message": "举报已提交，我们会尽快处理"})
        print(f"  [REPORT 多人] {player.name} 举报 #{target_num} {target_name}: {reason[:30]}")
        return

    # 单人模式举报
    room = game_manager.rooms.get(player.room_id)
    if not room:
        await player.send_json({"type": "error", "message": "你不在游戏中"})
        return
    opponent = room.get_opponent(player)
    opponent_name = opponent.name if not opponent.is_ai else (room.ai_instance.real_name if room.ai_instance else "AI")
    full_log = ""
    if room.conversation_log:
        full_log = "\n".join(f"{player.name if r=='user' else opponent_name}：{c}" for r, c in room.conversation_log)
    elif room.ai_instance and room.ai_instance.conversation_history:
        full_log = "\n".join(f"{player.name if r=='user' else opponent_name}：{c}" for r, c in room.ai_instance.conversation_history[-20:])
    from app.db import save_report, run_db
    opponent_ip = opponent.ip if hasattr(opponent, 'ip') and opponent.ip else ""
    reporter_ip = getattr(player, 'ip', '')
    await run_db(save_report, room.id, player.name, opponent_name, reason, "", full_log,
                reporter_ip=reporter_ip, offender_ip=opponent_ip)
    await player.send_json({"type": "report_submitted", "message": "举报已提交，我们会尽快处理"})
    print(f"  [REPORT] {player.name} (ip={reporter_ip}) 举报 {opponent_name} (ip={opponent_ip}): {reason[:30]}")


async def _handle_admin_login(player: Player, data: dict):
    """管理员登录，支持密码或token自动恢复"""
    password = data.get("password", "")
    token = data.get("token", "")
    import hashlib
    from app.main import SUPER_ADMIN_PASSWORD, SUPER_ADMIN_HASH, _hash_admin_pw

    admin_token = ""
    is_admin_user = False

    # 1. 检查超管密码
    if password == SUPER_ADMIN_PASSWORD or token == SUPER_ADMIN_HASH:
        admin_token = SUPER_ADMIN_HASH
        is_admin_user = True
        print(f"  [ADMIN] {player.name} (ip={player.ip}) 以超管身份登录")
    # 2. 检查小管理员密码：遍历所有管理员，用 SHA256 对比
    elif password:
        try:
            from app.db import list_admin_accounts
            hashed = _hash_admin_pw(password)
            for a in list_admin_accounts():
                if a.get("password_hash") and a["password_hash"] == hashed:
                    if a.get("is_active"):
                        admin_token = hashed
                        is_admin_user = True
                        print(f"  [ADMIN] {player.name} (ip={player.ip}) 以管理员({a['username']})身份登录")
                        break
        except Exception:
            pass
    # 3. 检查小管理员 token：遍历所有管理员，对比存储的 hash
    elif token:
        try:
            from app.db import list_admin_accounts
            for a in list_admin_accounts():
                if a.get("password_hash") and a["password_hash"] == token:
                    admin_token = token
                    is_admin_user = True
                    break
        except Exception:
            pass

    if is_admin_user:
        player.is_admin = True
        from app.db import log_admin_login, run_db
        uid = data.get("user_id", "")
        nickname = data.get("nickname", "") or player.name
        fingerprint = data.get("fingerprint", "")
        log_name = "root" if admin_token == SUPER_ADMIN_HASH and password == SUPER_ADMIN_PASSWORD else "admin"
        await run_db(log_admin_login, log_name, uid, nickname, player.ip or "", fingerprint)
        await player.send_json({"type": "admin_logged_in", "message": "管理员身份已激活", "admin_token": admin_token})
    else:
        await player.send_json({"type": "error", "message": "密码错误"})


async def _handle_admin_ban(player: Player, data: dict):
    if not player.is_admin:
        await player.send_json({"type": "error", "message": "无权限"})
        return
    room = game_manager.rooms.get(player.room_id)
    if not room:
        await player.send_json({"type": "error", "message": "你不在游戏中"})
        return
    opponent = room.get_opponent(player)
    if not opponent:
        await player.send_json({"type": "error", "message": "没有对手"})
        return
    reason = data.get("reason", "管理员封禁").strip() or "管理员封禁"
    duration = data.get("duration", "7天").strip() or "permanent"
    from app.db import add_banned_user, run_db, log_admin_action
    from datetime import datetime, timedelta
    import re
    expires_at = ""
    dur_match = re.match(r"^(\d+)\s*(分钟|分|min|m|小时|时|h|天|d|月|month)$", duration)
    if duration in ("永久", "permanent", "forever", ""):
        expires_at = ""
    elif dur_match:
        num = int(dur_match.group(1))
        unit = dur_match.group(2)
        if unit in ("分钟", "分", "min", "m"):
            expires_at = (datetime.now() + timedelta(minutes=num)).strftime("%Y-%m-%d %H:%M:%S")
        elif unit in ("小时", "时", "h"):
            expires_at = (datetime.now() + timedelta(hours=num)).strftime("%Y-%m-%d %H:%M:%S")
        elif unit in ("天", "d"):
            expires_at = (datetime.now() + timedelta(days=num)).strftime("%Y-%m-%d %H:%M:%S")
        elif unit in ("月", "month"):
            expires_at = (datetime.now() + timedelta(days=num * 30)).strftime("%Y-%m-%d %H:%M:%S")
    # 封禁 IP + 指纹 + user_id + 昵称（四个维度）
    fp = getattr(opponent, 'fingerprint', '')
    await run_db(add_banned_user, opponent.name, reason=reason, ip=opponent.ip or "",
                    user_id=opponent.user_id or "", expires_at=expires_at,
                    fingerprint=fp, banned_by=player.name)
    await player.send_json({"type": "admin_ban_ok", "message": f"已封禁 {opponent.name}"})
    await opponent.send_json({"type": "error", "message": "你已被封禁",
        "ban_user_id": opponent.user_id, "ban_reason": reason,
        "ban_expires": "封禁至 " + expires_at if expires_at else "永久封禁",})
    # 记录管理员操作日志（不添加到举报列表）
    await run_db(log_admin_action, player.id, player.name, "admin_ban",
                 f"封禁 {opponent.name} (ip={opponent.ip}, 理由={reason[:30]}, 时长={duration})")
    print(f"  [ADMIN-BAN] {player.name} 封禁 {opponent.name} (ip={opponent.ip}, 理由={reason[:20]})")


async def _send_matched(player: Player, room: Room, opponent: Player):
    msg = {"type": "matched", "room_id": room.id, "opponent_name": "用户", "is_ai": False}
    if player.is_admin and opponent:
        msg["admin_info"] = {"is_ai": opponent.is_ai, "ip": opponent.ip or "未知", "name": opponent.name, "user_id": opponent.user_id or ""}
    await player.send_json(msg)
async def _handle_msg_timeout(player: Player):
    """60秒内未发送消息自动判负"""
    # 取消可能还在排队的匹配任务
    if hasattr(player, '_match_task') and player._match_task and not player._match_task.done():
        player._match_task.cancel()
    room = game_manager.rooms.get(player.room_id)
    if not room or room.finished:
        return
    room.finished = True
    opponent = room.get_opponent(player)

    # 通知超时方
    await player.send_json({
        "type": "game_result", "result": "lose",
        "reason": "60秒内未发送消息，自动判负",
        "opponent_was_ai": opponent.is_ai if opponent else False,
        "your_guess": "timeout",
        "opponent_guess": "未判断",
        "your_real_name": player.name,
        "opponent_real_name": opponent.name if opponent else "?",
    })

    # 通知对方
    if opponent:
        await opponent.send_json({
            "type": "game_result", "result": "win",
            "reason": "对方未在60秒内发消息",
            "opponent_was_ai": True if hasattr(player, 'is_ai') and player.is_ai else False,
            "your_guess": "auto_win",
            "opponent_guess": "timeout",
            "your_real_name": opponent.name,
            "opponent_real_name": player.name,
        })

    game_manager.cleanup_room(room.id)
    print(f"  [TIMEOUT] {player.name} 未发消息自动判负")

async def _handle_multi_join(player, data):
    await handle_multi_join(player, data)

async def _handle_multi_leave(player):
    await handle_multi_leave(player)

async def _handle_multi_chat(player, data):
    await handle_multi_chat(player, data)

async def _handle_multi_sticker(player, data):
    await handle_multi_sticker(player, data)

async def _handle_multi_vote(player, data):
    await handle_multi_vote(player, data)

async def _handle_multi_admin_force(player):
    await handle_multi_admin_force(player)


# ─── 观战功能 ────────────────────────────────────────────────────

async def _handle_watch_list(player: Player, data: dict = None):
    """管理员请求当前所有在线对局列表，支持 mode 筛选: all/normal/multi"""
    if not player.is_admin:
        await player.send_json({"type": "error", "message": "无权限"})
        return
    mode = (data or {}).get("mode", "all")
    room_list = []

    # 普通模式对局
    if mode in ("all", "normal"):
        for rid, room in game_manager.rooms.items():
            if room.finished:
                continue
            elapsed = int((datetime.now() - room.start_time).total_seconds())
            p1_name = room.player1.name if not room.player1.is_ai else "AI"
            p2_name = room.player2.name if not room.player2.is_ai else "AI"
            p1_is_ai = room.player1.is_ai
            p2_is_ai = room.player2.is_ai
            room_list.append({
                "room_id": rid,
                "mode": "normal",
                "player1": {"name": p1_name, "is_ai": p1_is_ai, "id": room.player1.id, "ip": getattr(room.player1, 'ip', '')},
                "player2": {"name": p2_name, "is_ai": p2_is_ai, "id": room.player2.id, "ip": getattr(room.player2, 'ip', '')},
                "elapsed": elapsed,
                "status": room.status,
                "msg_count": max(room.player1.messages_sent, room.player2.messages_sent),
            })

    # 多人模式对局
    if mode in ("all", "multi"):
        from app.game.multiplayer import multi_rooms, multi_lobby
        # 大厅等待中
        real_lobby = [p for p in multi_lobby if not getattr(p, 'is_ai', False)]
        if real_lobby:
            lobby_names = [{"name": p.name, "is_ai": False, "id": p.id} for p in real_lobby]
            room_list.append({
                "room_id": "lobby",
                "mode": "multi",
                "players": lobby_names,
                "elapsed": 0,
                "status": "waiting",
                "msg_count": 0,
            })
        # 进行中的多人房间
        for rid, mroom in multi_rooms.items():
            if mroom.status == "finished":
                continue
            elapsed = int(time.time() - mroom.created_at)
            players_info = []
            for mp in mroom.players:
                players_info.append({
                    "name": mp.ref.name,
                    "is_ai": mp.is_ai,
                    "id": mp.ref.id if not mp.is_ai else mp.ref.id,
                    "num": mp.num,
                    "real_name": mp.real_name if mp.is_ai else "",
                })
            room_list.append({
                "room_id": f"m_{rid}",
                "mode": "multi",
                "players": players_info,
                "elapsed": elapsed,
                "status": mroom.status,
                "msg_count": mroom.chat_count,
                "total_players": len(mroom.players),
                "ai_count": mroom.ai_count,
                "round": mroom.round_num,
            })

    await player.send_json({"type": "watch_list", "rooms": room_list})


async def _handle_watch_start(player: Player, data: dict):
    """管理员开始观战某个对局"""
    if not player.is_admin:
        await player.send_json({"type": "error", "message": "无权限"})
        return
    room_id = data.get("room_id", "")
    if not room_id:
        await player.send_json({"type": "error", "message": "缺少 room_id"})
        return

    # 先退出之前的观战
    old_info = game_manager.get_watched_room_info(player.id)
    if old_info:
        game_manager.remove_watcher(old_info[1], player.id)

    # 多人模式大厅（无法观战聊天，仅提示）
    if room_id == "lobby":
        await player.send_json({"type": "error", "message": "大厅等待中，无聊天可观战"})
        return

    # 多人模式房间
    if room_id.startswith("m_"):
        from app.game.multiplayer import multi_rooms
        real_room_id = room_id[2:]  # 去掉 "m_" 前缀
        mroom = multi_rooms.get(real_room_id)
        if not mroom or mroom.status == "finished":
            await player.send_json({"type": "error", "message": "多人对局不存在或已结束"})
            return

        game_manager.add_watcher(room_id, player.id)

        # 构建全部用户信息
        all_players = []
        for mp in mroom.players:
            all_players.append({
                "name": mp.ref.name,
                "is_ai": mp.is_ai,
                "id": mp.ref.id if not mp.is_ai else mp.ref.id,
                "num": mp.num,
                "real_name": mp.real_name if mp.is_ai else "",
                "ip": getattr(mp.ref, 'ip', '') if not mp.is_ai else '',
            })

        elapsed = int(time.time() - mroom.created_at)
        await player.send_json({
            "type": "watch_started",
            "room_id": room_id,
            "mode": "multi",
            "players": all_players,
            "elapsed": elapsed,
            "status": mroom.status,
            "round": mroom.round_num,
            "ai_count": mroom.ai_count,
            "history": mroom.chat_log if mroom.chat_log else [],
        })
        return

    # 普通模式房间
    room = game_manager.rooms.get(room_id)
    if not room or room.finished:
        await player.send_json({"type": "error", "message": "对局不存在或已结束"})
        return

    game_manager.add_watcher(room_id, player.id)

    # 发送历史聊天记录
    history = []
    if room.ai_instance and room.ai_instance.conversation_history:
        # 确定哪个 player 是 AI，哪个是真人
        ai_player_name = room.player2.name if room.player2.is_ai else (room.player1.name if room.player1.is_ai else "")
        human_player_name = room.player1.name if not room.player1.is_ai else (room.player2.name if not room.player2.is_ai else "")
        for role, content in room.ai_instance.conversation_history:
            is_ai = (role == "assistant")
            history.append({
                "sender": ai_player_name if is_ai else human_player_name,
                "content": content,
                "is_ai": is_ai,
            })
    elif room.conversation_log:
        human_name = room.player1.name if not room.player1.is_ai else room.player2.name
        for role, content in room.conversation_log:
            history.append({
                "sender": human_name,
                "content": content,
                "is_ai": False,
            })

    p1_name = room.player1.name if not room.player1.is_ai else "AI"
    p2_name = room.player2.name if not room.player2.is_ai else "AI"
    elapsed = int((datetime.now() - room.start_time).total_seconds())

    await player.send_json({
        "type": "watch_started",
        "room_id": room_id,
        "mode": "normal",
        "player1": {"name": p1_name, "is_ai": room.player1.is_ai, "id": room.player1.id, "ip": getattr(room.player1, 'ip', '')},
        "player2": {"name": p2_name, "is_ai": room.player2.is_ai, "id": room.player2.id, "ip": getattr(room.player2, 'ip', '')},
        "elapsed": elapsed,
        "history": history,
    })


async def _handle_watch_stop(player: Player):
    """管理员停止观战"""
    room_id = game_manager.get_watched_room(player.id)
    if room_id:
        game_manager.remove_watcher(room_id, player.id)
    await player.send_json({"type": "watch_stopped"})


async def _handle_watch_ban(player: Player, data: dict):
    """管理员在观战时对单个用户进行封禁"""
    if not player.is_admin:
        await player.send_json({"type": "error", "message": "无权限"})
        return
    target_id = data.get("target_id", "")
    target_name = data.get("target_name", "")
    reason = data.get("reason", "管理员封禁")
    duration = data.get("duration", "1小时")
    room_id = data.get("room_id", "")

    if not target_id and not target_name:
        await player.send_json({"type": "error", "message": "缺少目标用户"})
        return

    from app.db import run_db, add_banned_user, log_admin_action

    # 解析 duration → expires
    expires = ""
    if duration and duration not in ("永久", "permanent", "forever"):
        m = re.match(r'(\d+)(分钟|小时|天|永久)', str(duration))
        if m:
            num = int(m.group(1))
            unit = m.group(2)
            delta_map = {"分钟": timedelta(minutes=num), "小时": timedelta(hours=num), "天": timedelta(days=num)}
            expires = (datetime.now() + delta_map.get(unit, timedelta(hours=1))).strftime("%Y-%m-%d %H:%M:%S")
        else:
            expires = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

    # 封禁用户（四个维度：name/ip/user_id/fingerprint）
    try:
        await run_db(
            add_banned_user,
            target_name or target_id,
            reason,
            "",
            target_id,
            player.name,
            expires,
        )
    except Exception:
        pass

    # 记录管理员操作日志（不添加到举报列表）
    try:
        await run_db(log_admin_action, player.id, player.name, "watch_ban",
                     f"封禁用户 {target_name}({target_id}), 理由:{reason}, 时长:{duration}")
    except Exception:
        pass

    # 尝试踢掉目标用户
    target_player = game_manager.players.get(target_id)
    if not target_player:
        # 按名字查找
        for pid, pl in game_manager.players.items():
            if pl.name == target_name:
                target_player = pl
                break
    if target_player and target_player.websocket:
        try:
            await target_player.send_json({
                "type": "error",
                "message": f"你已被管理员封禁\n原因：{reason}\n时长：{duration}",
            })
        except Exception:
            pass

    # 通知观战者
    game_manager.notify_watchers(room_id, {
        "type": "watch_system",
        "room_id": room_id,
        "message": f"管理员封禁了 {target_name}（原因：{reason}，时长：{duration}）",
    })

    await player.send_json({
        "type": "watch_ban_ok",
        "message": f"已封禁 {target_name}",
        "target_id": target_id,
    })


async def _handle_watch_warn(player: Player, data: dict):
    """管理员在观战时向房间内发送警告（所有人都能看见）"""
    if not player.is_admin:
        await player.send_json({"type": "error", "message": "无权限"})
        return
    room_id = data.get("room_id", "")
    message = data.get("message", "").strip()
    if not message:
        await player.send_json({"type": "error", "message": "警告内容不能为空"})
        return

    from app.db import run_db, add_warning, log_admin_action

    warn_data = {
        "type": "watch_warn",
        "room_id": room_id,
        "message": f"[管理员警告] {message}",
        "admin_name": player.name,
    }

    # 普通模式房间
    if not room_id.startswith("m_"):
        room = game_manager.rooms.get(room_id)
        if room:
            # 发给房间内两个玩家
            for p in (room.player1, room.player2):
                if p.websocket:
                    try:
                        await p.send_json(warn_data)
                    except Exception:
                        pass
            # 记录警告到 DB（用 run_db 包裹）
            for p in (room.player1, room.player2):
                if not p.is_ai:
                    try:
                        await run_db(add_warning, p.user_id or p.id, message, player.name, p.name, getattr(p, 'ip', ''))
                    except Exception:
                        pass
    else:
        # 多人模式房间
        from app.game.multiplayer import multi_rooms
        real_room_id = room_id[2:]
        mroom = multi_rooms.get(real_room_id)
        if mroom:
            for mp in mroom.players:
                if not mp.is_ai and mp.ref.websocket:
                    try:
                        await mp.ref.send_json(warn_data)
                    except Exception:
                        pass
                if not mp.is_ai:
                    try:
                        await run_db(add_warning, mp.ref.user_id or mp.ref.id, message, player.name, mp.ref.name, getattr(mp.ref, 'ip', ''))
                    except Exception:
                        pass

    # 记录管理员操作日志
    try:
        await run_db(log_admin_action, player.id, player.name, "watch_warn",
                     f"向房间 {room_id} 发送警告: {message[:50]}")
    except Exception:
        pass

    # 通知观战者自己
    game_manager.notify_watchers(room_id, {
        "type": "watch_system",
        "room_id": room_id,
        "message": f"已发送警告给房间内所有人：{message}",
    })

    await player.send_json({
        "type": "watch_warn_ok",
        "message": "警告已发送",
    })


async def _handle_recovery_login(player: Player, data: dict):
    """用户使用恢复码登录，恢复账号身份"""
    code = (data.get("code") or "").strip().upper()
    if not code:
        await player.send_json({"type": "error", "message": "请输入恢复码"})
        return
    try:
        from app.db import verify_recovery_code, generate_recovery_code
        info = verify_recovery_code(code)
        if not info:
            await player.send_json({"type": "error", "message": "恢复码无效或已过期"})
            return
        # 恢复身份
        old_user_id = player.user_id
        player.user_id = info["user_id"]
        player.name = info["nickname"] or player.name
        # 刷新恢复码（保持有效）
        code = generate_recovery_code(info["user_id"], player.name, info.get("ip", ""))
        await player.send_json({
            "type": "recovery_logged_in",
            "user_id": info["user_id"],
            "nickname": info["nickname"],
            "code": code,
        })
        print(f"  [RECOVERY] 用户 {player.id} 使用恢复码登录: {info['nickname']} ({info['user_id'][:12]})")
    except Exception as e:
        print(f"  [RECOVERY ERROR] {e}")
        await player.send_json({"type": "error", "message": "恢复码验证失败"})


async def _handle_get_recovery_code(player: Player, data: dict = None):
    """用户请求自己的恢复码（若没有则自动生成）"""
    uid = player.user_id
    if not uid and data:
        uid = data.get("user_id", "").strip()
    if not uid:
        await player.send_json({"type": "error", "message": "请先设置昵称"})
        return
    # 同步 user_id 到 player 对象
    if uid != player.user_id:
        player.user_id = uid
    try:
        from app.db import get_recovery_code_by_user_id, generate_recovery_code
        code = get_recovery_code_by_user_id(uid)
        if not code:
            code = generate_recovery_code(uid, player.name, getattr(player, 'ip', ''))
        if code:
            await player.send_json({
                "type": "recovery_code",
                "code": code,
                "user_id": uid,
                "nickname": player.name,
            })
    except Exception:
        pass


async def _handle_change_nickname(player: Player, data: dict):
    """用户修改昵称（30天冷却）"""
    new_nickname = (data.get("new_nickname") or "").strip()
    if not new_nickname:
        await player.send_json({"type": "error", "message": "请输入新昵称"})
        return
    if len(new_nickname) > 20:
        await player.send_json({"type": "error", "message": "昵称过长（最多20字）"})
        return
    if not player.user_id:
        await player.send_json({"type": "error", "message": "请先设置昵称"})
        return
    from app.db import change_nickname as _change_nick
    success, msg = _change_nick(player.user_id, player.name, new_nickname, player.ip)
    if success:
        old_name = player.name
        player.name = new_nickname
        await player.send_json({
            "type": "nickname_changed",
            "old_nickname": old_name,
            "new_nickname": new_nickname,
            "message": msg,
        })
    else:
        await player.send_json({"type": "error", "message": msg})
