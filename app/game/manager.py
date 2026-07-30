"""
图灵测试 · 游戏状态管理
Player → Room → GameManager 三层模型
"""
import asyncio
import random
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import WebSocket

from app.config import GAME_CONFIG

from app.game.ai_player import AiPlayer


class Player:
    """玩家连接"""

    def __init__(self, websocket: "WebSocket | None", client_ip: str = ""):
        self.id = uuid.uuid4().hex[:8]
        self.websocket = websocket
        self.name = f"玩家{random.randint(1000, 9999)}"
        self.ip = client_ip
        self.user_id = ""
        self.fingerprint = ""
        self.is_admin = False
        self.room_id: Optional[str] = None
        self.judgment_made = False
        self.judgment_choice: Optional[str] = None
        self.messages_sent = 0
        self.is_ai = False

    async def send_json(self, data: dict):
        """发送 JSON 消息（5 秒超时，防止慢客户端卡住协程）"""
        try:
            if self.websocket:
                await asyncio.wait_for(self.websocket.send_json(data), timeout=5.0)
        except asyncio.TimeoutError:
            pass  # 客户端太慢，丢弃消息
        except Exception:
            pass


class Room:
    """游戏房间"""

    def __init__(self, room_id: str, player1: Player, player2: Player):
        self.id = room_id
        self.player1 = player1
        self.player2 = player2
        self.player1.room_id = room_id
        self.player2.room_id = room_id
        self.status = "chatting"
        self.judger: Optional[Player] = None
        self.judgment_timer: Optional[asyncio.Task] = None
        self.start_time = datetime.now()
        self.finished = False
        self.ai_instance: Optional[AiPlayer] = None
        self._reply_task: Optional[asyncio.Task] = None  # 当前生成任务（用于打断）
        self.ai_has_judged = False  # AI 是否已主动判断
        self.total_timer: Optional[asyncio.Task] = None  # 10 分钟总超时
        self._aggressive_flag = False  # 对方语言激烈标记
        self._aggressive_msg = ""
        self.conversation_log: list = []  # 真人对话记录 [(role, content), ...]

    def get_opponent(self, player: Player) -> Player:
        return self.player2 if player.id == self.player1.id else self.player1

    async def broadcast(self, data: dict, exclude: Player = None):
        for p in (self.player1, self.player2):
            if p != exclude and not p.is_ai:
                await p.send_json(data)

    async def send_to(self, player: Player, data: dict):
        await player.send_json(data)

    async def start_judgment_timer(self, judger: Player):
        async def timeout():
            await asyncio.sleep(GAME_CONFIG["judgment_timeout"])
            opponent = self.get_opponent(judger)
            if not opponent.judgment_made:
                if opponent.is_ai and self.ai_has_judged:
                    # AI 已判断过，玩家超时 — 直接结算
                    await self.send_to(judger, {
                        "type": "game_result", "result": "lose",
                        "reason": "判断超时，AI 已先判断了你",
                        "opponent_was_ai": True, "your_guess": "timeout",
                        "opponent_real_name": self.ai_instance.real_name if self.ai_instance else "?",
                        "your_real_name": judger.name,
                    })
                else:
                    await self.send_to(judger, {
                        "type": "game_result", "result": "win",
                        "reason": "对方未在规定时间内做出判断",
                        "opponent_was_ai": False, "your_guess": judger.judgment_choice,
                        "opponent_guess": "timeout",
                    })
                    await self.send_to(opponent, {
                        "type": "game_result", "result": "lose",
                        "reason": "判断超时",
                        "opponent_was_ai": False, "your_guess": "timeout",
                        "opponent_guess": "human" if judger.judgment_choice == "human" else "ai",
                    })
                self.finished = True
                await self.cancel_total_timer()

        self.judgment_timer = asyncio.create_task(timeout())

    async def cancel_timer(self):
        if self.judgment_timer:
            self.judgment_timer.cancel()
            self.judgment_timer = None

    async def start_total_timer(self):
        """10 分钟总超时，不管聊没聊完都结束"""
        async def _timeout():
            await asyncio.sleep(600)  # 10 分钟
            if not self.finished:
                self.finished = True
                await self.broadcast({
                    "type": "game_result",
                    "result": "timeout",
                    "reason": "10 分钟时间到，自动结算",
                    "opponent_was_ai": False,
                })
        self.total_timer = asyncio.create_task(_timeout())

    async def cancel_total_timer(self):
        if self.total_timer:
            self.total_timer.cancel()
            self.total_timer = None


DISCONNECT_GRACE_SECONDS = 15  # 断连重连窗口期

class GameManager:
    """游戏全局管理器"""

    def __init__(self):
        self.waiting_queue: list[Player] = []
        self.rooms: dict[str, Room] = {}
        self.players: dict[str, Player] = {}
        self.ai_sessions: dict[str, AiPlayer] = {}
        # 观战者映射: room_id -> set of admin player_ids
        self.watchers: dict[str, set[str]] = {}
        # 断连等待重连: user_id -> {room_id, slot: "player1"/"player2", player_info, expires_at, task}
        self.disconnected_games: dict[str, dict] = {}

    def add_watcher(self, room_id: str, admin_player_id: str):
        if room_id not in self.watchers:
            self.watchers[room_id] = set()
        self.watchers[room_id].add(admin_player_id)

    def remove_watcher(self, room_id: str, admin_player_id: str):
        if room_id in self.watchers:
            self.watchers[room_id].discard(admin_player_id)
            if not self.watchers[room_id]:
                del self.watchers[room_id]

    def remove_all_watchers_for(self, admin_player_id: str):
        """管理员断线时清除所有观战"""
        for room_id in list(self.watchers.keys()):
            self.watchers[room_id].discard(admin_player_id)
            if not self.watchers[room_id]:
                del self.watchers[room_id]

    def get_watched_room(self, admin_player_id: str) -> Optional[str]:
        """获取管理员正在观战的房间ID"""
        for room_id, watchers in self.watchers.items():
            if admin_player_id in watchers:
                return room_id
        return None

    def notify_watchers(self, room_id: str, data: dict):
        """通知某个房间的所有观战者（异步，非阻塞）"""
        watcher_ids = self.watchers.get(room_id, set()).copy()
        for wid in watcher_ids:
            player = self.players.get(wid)
            if player and player.websocket:
                asyncio.ensure_future(player.send_json(data))

    def get_watched_room_type(self, admin_player_id: str) -> str:
        """获取管理员正在观战的房间类型: 'normal' or 'multi'"""
        for room_id, watchers in self.watchers.items():
            if admin_player_id in watchers:
                if room_id.startswith("m_"):
                    return "multi"
                return "normal"
        return ""

    def get_watched_room_info(self, admin_player_id: str) -> Optional[tuple]:
        """返回 (room_type, room_id)"""
        for room_id, watchers in self.watchers.items():
            if admin_player_id in watchers:
                if room_id.startswith("m_"):
                    return ("multi", room_id)
                return ("normal", room_id)
        return None

    def register_player(self, player: Player):
        self.players[player.id] = player

    def unregister_player(self, player_id: str):
        player = self.players.pop(player_id, None)
        if not player:
            return
        if player in self.waiting_queue:
            self.waiting_queue.remove(player)
        if player.room_id and not player.is_ai:
            room = self.rooms.get(player.room_id)
            if room and not room.finished:
                opponent = room.get_opponent(player)
                if opponent and not opponent.is_ai:
                    # 1v1 真人对战：给 15 秒重连窗口
                    self._start_disconnect_grace(player, room, "player1" if room.player1 is player else "player2")
                    return
                # 对手是 AI → 玩家断连直接判负
                if opponent and opponent.is_ai:
                    room.finished = True
                    asyncio.ensure_future(player.send_json({
                        "type": "game_result", "result": "lose",
                        "reason": "你断开了连接",
                        "opponent_was_ai": True,
                    }))
                    self.cleanup_room(room.id)
                    return
            if room:
                self.cleanup_room(room.id)

    def _start_disconnect_grace(self, player: Player, room: Room, slot: str):
        """给断连玩家 15 秒重连窗口"""
        user_id = player.user_id
        if not user_id:
            # 没有 user_id 无法重连，直接判负
            asyncio.ensure_future(self._handle_disconnect_timeout(user_id, room, slot))
            return

        async def _grace_timeout():
            await asyncio.sleep(DISCONNECT_GRACE_SECONDS)
            await self._handle_disconnect_timeout(user_id, room, slot)

        task = asyncio.create_task(_grace_timeout())
        self.disconnected_games[user_id] = {
            "room_id": room.id,
            "slot": slot,
            "player_info": {
                "name": player.name,
                "ip": player.ip,
                "fingerprint": player.fingerprint,
                "messages_sent": player.messages_sent,
                "judgment_made": player.judgment_made,
                "judgment_choice": player.judgment_choice,
            },
            "expires_at": time.time() + DISCONNECT_GRACE_SECONDS,
            "task": task,
        }
        # 通知对手
        opponent = room.get_opponent(player)
        if opponent and not opponent.is_ai:
            asyncio.ensure_future(opponent.send_json({
                "type": "opponent_disconnected",
                "message": f"对手断开了连接，等待重连（{DISCONNECT_GRACE_SECONDS}秒）...",
                "grace_seconds": DISCONNECT_GRACE_SECONDS,
            }))

    async def _handle_disconnect_timeout(self, user_id: str, room: Room, slot: str):
        """断连超时：对手获胜"""
        # 检查房间是否还在
        if room.id not in self.rooms:
            if user_id in self.disconnected_games:
                del self.disconnected_games[user_id]
            return
        if room.finished:
            if user_id in self.disconnected_games:
                del self.disconnected_games[user_id]
            return
        room.finished = True
        # 通知对手获胜
        opponent = room.get_opponent(room.player1 if slot == "player1" else room.player2)
        if opponent and not opponent.is_ai:
            asyncio.ensure_future(opponent.send_json({
                "type": "game_result", "result": "win",
                "reason": "对手断开连接超时未重连，你赢了！",
                "opponent_was_ai": False,
            }))
        # 清理
        if user_id in self.disconnected_games:
            del self.disconnected_games[user_id]
        self.cleanup_room(room.id)

    def try_reconnect(self, user_id: str, new_player: Player) -> bool:
        """尝试让断连玩家重连到之前的房间，返回是否成功"""
        info = self.disconnected_games.get(user_id)
        if not info:
            return False
        if time.time() > info["expires_at"]:
            # 已超时，清理
            if info.get("task") and not info["task"].done():
                info["task"].cancel()
            # 房间可能已被清理（对手已判赢）
            # 给玩家发送失败消息
            asyncio.ensure_future(new_player.send_json({
                "type": "game_result",
                "result": "lose",
                "reason": "你断开了连接，未能在规定时间内重连",
                "opponent_was_ai": False,
            }))
            del self.disconnected_games[user_id]
            return False

        room = self.rooms.get(info["room_id"])
        if not room or room.finished:
            if info.get("task") and not info["task"].done():
                info["task"].cancel()
            asyncio.ensure_future(new_player.send_json({
                "type": "game_result",
                "result": "lose",
                "reason": "你断开了连接，未能在规定时间内重连",
                "opponent_was_ai": False,
            }))
            del self.disconnected_games[user_id]
            return False

        # 取消超时任务
        if info.get("task") and not info["task"].done():
            info["task"].cancel()

        slot = info["slot"]
        p_info = info["player_info"]

        # 恢复玩家到房间
        new_player.room_id = room.id
        new_player.name = p_info["name"]
        new_player.messages_sent = p_info["messages_sent"]
        new_player.judgment_made = p_info["judgment_made"]
        new_player.judgment_choice = p_info["judgment_choice"]

        if slot == "player1":
            room.player1 = new_player
        else:
            room.player2 = new_player

        # 从断连列表中移除
        del self.disconnected_games[user_id]

        # 通知对手重连成功
        opponent = room.get_opponent(new_player)
        if opponent and not opponent.is_ai:
            asyncio.ensure_future(opponent.send_json({
                "type": "opponent_reconnected",
                "message": "对手已重新连接！游戏继续",
            }))

        return True

    def cleanup_room(self, room_id: str):
        room = self.rooms.pop(room_id, None)
        if room:
            for p in (room.player1, room.player2):
                if p.id in self.ai_sessions:
                    del self.ai_sessions[p.id]
            asyncio.ensure_future(room.cancel_timer())
            asyncio.ensure_future(room.cancel_total_timer())
            # 取消进行中的 AI 回复任务，防止资源泄漏
            if hasattr(room, '_reply_task') and room._reply_task and not room._reply_task.done():
                room._reply_task.cancel()
            # 清理该房间的断连等待记录
            for uid in list(self.disconnected_games.keys()):
                if self.disconnected_games[uid]["room_id"] == room_id:
                    task = self.disconnected_games[uid].get("task")
                    if task and not task.done():
                        task.cancel()
                    del self.disconnected_games[uid]
        # 清理观战者
        if room_id in self.watchers:
            watcher_ids = self.watchers.pop(room_id, set()).copy()
            for wid in watcher_ids:
                player = self.players.get(wid)
                if player and player.websocket:
                    asyncio.ensure_future(player.send_json({
                        "type": "watch_ended", "room_id": room_id, "reason": "对局已结束"
                    }))

    def match_player_with_ai(self, player: Player) -> Room:
        room_id = f"room_{uuid.uuid4().hex[:8]}"
        ai_slot = Player(None)
        ai_slot.id = f"ai_{uuid.uuid4().hex[:8]}"
        ai_slot.is_ai = True
        ai_slot.room_id = room_id
        ai = AiPlayer()
        ai_slot.name = ai.name
        self.ai_sessions[ai_slot.id] = ai
        room = Room(room_id, player, ai_slot)
        room.ai_instance = ai
        self.rooms[room_id] = room
        return room

    def match_players(self, player1: Player, player2: Player) -> Room:
        room_id = f"room_{uuid.uuid4().hex[:8]}"
        room = Room(room_id, player1, player2)
        self.rooms[room_id] = room
        return room

    def try_match(self, player: Player) -> Optional[Room]:
        if self.waiting_queue:
            opponent = self.waiting_queue.pop(0)
            return self.match_players(player, opponent)
        return None


# 全局游戏管理器实例（单例）
game_manager = GameManager()
