"""
图灵测试 · FastAPI 主应用
路由定义 + WebSocket + 管理面板 API
"""
import json
import hashlib
import os
import asyncio
import secrets
import subprocess
import random
import uuid

import httpx
from fastapi import FastAPI, WebSocket, Request, HTTPException, Depends, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBearer
from starlette.websockets import WebSocketDisconnect

from app.config import AI_CONFIG, GAME_CONFIG, DEFAULT_CONFIG, load_config, save_config, CONFIG_FILE
from app.game.manager import Player, game_manager
from app.game.websocket_handler import handle_player

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or AI_CONFIG.get("admin_password", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = "11451410086Asd."
ADMIN_TOKEN = hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()
TOKEN_COOKIE = "admin_token"

# 超管（与面板管理员共用同一密码，可按需分离）
SUPER_ADMIN_PASSWORD = ADMIN_PASSWORD
SUPER_ADMIN_HASH = ADMIN_TOKEN

# uvicorn server 引用（供控制台命令触发优雅关闭）
uvicorn_server = None


def _hash_admin_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

# ---- 创建 FastAPI 应用 ----
app = FastAPI(title="图灵测试 · Turing Test")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---- 在线人数历史（用于折线图） ----
import threading as _threading_mod
_online_history = []  # [{"time": "HH:MM", "count": N}, ...]
_online_history_lock = _threading_mod.Lock()

def _record_online_count():
    """每 60 秒记录一次在线人数（游戏 + 聊天大厅）"""
    import time as _time_mod
    from datetime import datetime
    while True:
        _time_mod.sleep(60)
        try:
            from app.game.manager import game_manager
            game_players = sum(1 for p in game_manager.players.values() if not p.is_ai)
            with _chat_lobby_lock:
                chat_lobby_count = len(_chat_lobby_users)
            count = game_players + chat_lobby_count
            now = datetime.now().strftime("%H:%M")
            with _online_history_lock:
                _online_history.append({"time": now, "count": count})
                # 保留 24 小时（1440 条）
                if len(_online_history) > 1440:
                    del _online_history[:len(_online_history) - 1440]
        except Exception:
            pass


@app.on_event("shutdown")
async def _on_shutdown():
    from app.ai.llm import _close_client
    await _close_client()
    print("  [SHUTDOWN] HTTP 连接池已关闭")


# 管理面板测试会话
_admin_test_session = None


def verify_admin(request: Request):
    """验证管理员身份（cookie 中的 token）"""
    token = request.cookies.get(TOKEN_COOKIE, "")
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="需要管理员密码")
    return True


def _is_super_admin(request: Request):
    """验证是否为超级管理员"""
    token = request.cookies.get("admin_token", "")
    if token == SUPER_ADMIN_HASH:
        return True
    # 也支持通过密码验证
    pw = request.headers.get("X-Admin-Password", "")
    if pw == SUPER_ADMIN_PASSWORD:
        return True
    raise HTTPException(status_code=403, detail="需要超级管理员权限")


def _is_any_admin(request: Request):
    """验证是否为任何管理员（超管或小管理员）"""
    token = request.cookies.get("admin_token", "")
    # 检查超管密码
    if token == SUPER_ADMIN_HASH:
        return True
    # 检查小管理员 token：遍历所有管理员，对比 token 是否匹配其密码哈希
    if token:
        try:
            from app.db import list_admin_accounts
            for a in list_admin_accounts():
                if a.get("password_hash") and a["password_hash"] == token:
                    return True
        except Exception as e:
            import traceback
            print(f"  [WARN] _is_any_admin 检查失败: {e}\n{traceback.format_exc()}")
    raise HTTPException(status_code=403, detail="需要管理员权限")


@app.post("/api/login")
async def login(request: Request):
    """管理面板登录"""
    pw = ""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            body = await request.json()
            pw = body.get("password", "")
        except Exception:
            pass
    else:
        try:
            form = await request.form()
            pw = form.get("password", "")
        except Exception:
            pass
    if pw == ADMIN_PASSWORD:
        from fastapi.responses import RedirectResponse
        resp = RedirectResponse(url="/admin", status_code=303)
        resp.set_cookie(TOKEN_COOKIE, ADMIN_TOKEN, path="/", max_age=86400)
        return resp
    # 密码错误 → 重定向回登录页带错误
    from fastapi.responses import RedirectResponse
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie("login_error", "1", path="/", max_age=60)
    return resp


# ---- 页面路由 ----
@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/admin")
async def admin_page(request: Request):
    token = request.cookies.get(TOKEN_COOKIE, "")
    if token != ADMIN_TOKEN:
        return FileResponse("static/login.html")
    return FileResponse("static/admin.html")


# ---- 配置 API ----
@app.get("/api/config", dependencies=[Depends(verify_admin)])
async def get_config():
    """获取当前配置（api_key 仅返回是否设置，不回显明文）"""
    return dict(AI_CONFIG)


@app.post("/api/config", dependencies=[Depends(verify_admin)])
async def update_config(request: Request):
    """更新配置并持久化到 config.json"""
    global AI_CONFIG, GAME_CONFIG
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效的 JSON"}, status_code=400)

    valid_keys = set(DEFAULT_CONFIG.keys())
    for k, v in body.items():
        if k in valid_keys:
            AI_CONFIG[k] = v
    GAME_CONFIG = AI_CONFIG
    save_config(AI_CONFIG)
    return {"status": "ok", "config": AI_CONFIG}


@app.post("/api/test", dependencies=[Depends(verify_admin)])
async def test_config():
    """用当前配置验证连通性"""
    from app.ai.llm import test_connection
    if not AI_CONFIG.get("api_key"):
        return JSONResponse({"status": "error", "message": "请先填写 API Key"}, status_code=400)
    try:
        reply = await test_connection()
        return {"status": "ok", "reply": reply}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/chat", dependencies=[Depends(verify_admin)])
async def debug_chat(request: Request):
    """管理面板测试聊天：SSE 流式输出三阶段流水线"""
    from app.game.ai_player import AiPlayer
    from app.ai.llm import chat_completion
    from app.db import get_learned_styles, get_learned_jargon, replace_learned_styles, replace_learned_jargon
    from app.ai.learner import learn as do_learn
    global _admin_test_session

    if not AI_CONFIG.get("api_key"):
        return JSONResponse({"status": "error", "message": "请先填写 API Key"}, status_code=400)

    try:
        body = await request.json()
        message = body.get("message", "").strip()
        action = body.get("action", "send")
    except Exception:
        return JSONResponse({"status": "error", "message": "无效的 JSON"}, status_code=400)

    if action == "reset":
        _admin_test_session = None
        return {"status": "ok", "cleared": True}

    if not message:
        return JSONResponse({"status": "error", "message": "消息不能为空"}, status_code=400)

    if _admin_test_session is None:
        _admin_test_session = AiPlayer()

    async def event_stream():
        ai = _admin_test_session

        # 学习
        ai._user_msg_count += 1
        if ai._user_msg_count >= 2 and ai._user_msg_count % 2 == 0:
            user_msgs = [c for r, c in ai.conversation_history if r == "user"]
            user_msgs.append(message)
            try:
                exprs, jargon = await do_learn(user_msgs)
                if exprs:
                    ai.learned_expressions = exprs
                    replace_learned_styles(ai.session_id, exprs)
                if jargon:
                    ai.learned_jargon = jargon
                    replace_learned_jargon(ai.session_id, jargon)
            except Exception:
                pass

        # 构建 prompt
        system_parts = [AI_CONFIG.get("system_prompt", "")]
        if ai.learned_expressions or ai.learned_jargon:
            lines = ["【观察到的对方习惯】"]
            if ai.learned_expressions:
                lines.append("；".join(f"当『{s}』→『{t}』" for s, t in ai.learned_expressions[:4]))
            if ai.learned_jargon:
                lines.append("常用词：" + "、".join(ai.learned_jargon[:6]))
            system_parts.append("\n".join(lines))
        system_parts.append("【回复风格要求】\n一句话，15-30字。不要 emoji。不要承认是 AI。不要报固定名字。")
        system_prompt = "\n\n".join(system_parts)
        msgs = [{"role": "system", "content": system_prompt}]
        for role, content in ai.conversation_history:
            msgs.append({"role": role, "content": content})

        # Planner
        plan = ""
        try:
            plan = await chat_completion(
                [{"role": "system", "content": "分析这个聊天场景，给出决策建议。具体自然，不要固定格式。"},
                 {"role": "user", "content": message}],
                max_tokens_override=200,
            )
            yield f"data: {json.dumps({'phase':'plan','content':plan}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'phase':'error','content':str(e)}, ensure_ascii=False)}\n\n"

        if plan:
            msgs.append({"role": "user", "content": f"【回复信息参考】\n当前思考：\n{plan}"})
        msgs.append({"role": "user", "content": message})

        # Replyer
        try:
            raw = await chat_completion(msgs)
            yield f"data: {json.dumps({'phase':'raw','content':raw}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'phase':'error','content':str(e)}, ensure_ascii=False)}\n\n"
            return

        # Expressor
        try:
            expr_msgs = [
                {"role": "system", "content": "改写得更口语化，不要 emoji，不要引号，不要标点包裹，不要报名字。只输出一句话，≤30字。"},
                {"role": "user", "content": raw},
            ]
            expressed = await chat_completion(expr_msgs, max_tokens_override=80)
            expressed = expressed.strip().strip('"').strip("'").strip("“”").strip()
            reply = expressed if expressed else raw
            yield f"data: {json.dumps({'phase':'express','content':expressed or '', 'reply':reply}, ensure_ascii=False)}\n\n"
        except Exception:
            reply = raw
            yield f"data: {json.dumps({'phase':'express','content':'', 'reply':reply}, ensure_ascii=False)}\n\n"

        # 持久化
        ai.conversation_history.append(("user", message))
        ai.conversation_history.append(("assistant", reply))
        from app.db import add_conversation
        add_conversation(ai.session_id, "user", message)
        add_conversation(ai.session_id, "assistant", reply)

        yield f"data: {json.dumps({'phase':'done','reply':reply}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/reports", dependencies=[Depends(verify_admin)])
async def list_reports(request: Request):
    """获取举报列表，支持筛选"""
    from app.db import get_reports
    status_filter = request.query_params.get("status", "")
    search = request.query_params.get("search", "")
    all_reports = get_reports(100)
    if status_filter:
        all_reports = [r for r in all_reports if r.get("status") == status_filter]
    if search:
        s = search.lower()
        all_reports = [r for r in all_reports if
                       s in r.get("reporter", "").lower() or
                       s in r.get("offender", "").lower() or
                       s in r.get("reason", "").lower()]
    return {"reports": all_reports, "total": len(all_reports)}


@app.get("/api/reports/stats", dependencies=[Depends(verify_admin)])
async def report_stats():
    """举报统计数据"""
    from app.db import _get_conn
    conn = _get_conn()
    total = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
    pending = conn.execute("SELECT count(*) FROM reports WHERE status='pending'").fetchone()[0]
    banned = conn.execute("SELECT count(*) FROM reports WHERE status='ban'").fetchone()[0]
    dismissed = conn.execute("SELECT count(*) FROM reports WHERE status='dismiss'").fetchone()[0]
    return {"total": total, "pending": pending, "banned": banned, "dismissed": dismissed}


@app.post("/api/reports/action", dependencies=[Depends(verify_admin)])
async def report_action(request: Request):
    """封禁/解封/忽略举报，同时操作 banned_users 表"""
    from app.db import update_report_status, add_banned_user, get_reports
    from datetime import datetime, timedelta
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    rid = body.get("id")
    action = body.get("action")  # ban / dismiss / unban
    if not rid or action not in ("ban", "dismiss", "unban"):
        return JSONResponse({"status": "error", "message": "参数错误"}, status_code=400)

    # 操作前先取到被举报人信息
    reports = get_reports(100)
    target = None
    target_ip = ""
    for r in reports:
        if r["id"] == rid:
            target = r.get("offender") or r.get("reporter") or ""
            target_ip = r.get("offender_ip", "")
            break

    update_report_status(rid, action)

    # 封禁/解封同步到 banned_users 表（四个维度：name/ip/user_id/fingerprint）
    if action == "ban" and target:
        ban_reason = body.get("ban_reason", "").strip() or f"举报 #{rid}"
        ban_duration = body.get("ban_duration", "").strip().lower()
        expires_at = ""
        import re
        dur_match = re.match(r"^(\d+)\s*(分钟|分|min|m|小时|时|h|天|d|月|month)$", ban_duration)
        if ban_duration in ("permanent", "永久", "forever", ""):
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
        target_uid = body.get("offender_user_id", "")
        target_fp = body.get("offender_fingerprint", "")
        add_banned_user(target, reason=ban_reason, ip=target_ip, user_id=target_uid,
                        expires_at=expires_at, fingerprint=target_fp, banned_by="举报处理")
        print(f"  [BAN] 封禁: {target} (ip={target_ip}, dur={ban_duration}, 理由={ban_reason[:20]})")
    elif action == "unban" and target:
        from app.db import _get_conn
        conn = _get_conn()
        conn.execute("DELETE FROM banned_users WHERE name=? AND ip=?", (target, target_ip))
        conn.commit()
        print(f"  [BAN] 解封: {target} (ip={target_ip})")

    return {"status": "ok"}


@app.post("/api/reports/clear", dependencies=[Depends(verify_admin)])
async def clear_reports():
    """清空所有举报记录"""
    from app.db import clear_all_reports as _clear
    _clear()
    print("  [REPORT] 管理员清空了所有举报记录")
    return {"status": "ok", "message": "所有举报记录已清空"}


@app.get("/api/banned", dependencies=[Depends(verify_admin)])
async def list_banned():
    """封禁名单"""
    from app.db import get_banned_users
    return {"users": get_banned_users()}


@app.post("/api/banned/manual", dependencies=[Depends(verify_admin)])
async def manual_ban(request: Request):
    """手动封禁/解封"""
    from app.db import add_banned_user
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    action = body.get("action", "")
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"status": "error", "message": "请输入用户名"}, status_code=400)
    if action == "ban":
        reason = body.get("reason", "管理员封禁")
        uid = body.get("user_id", "")
        ip = body.get("ip", "")
        fp = body.get("fingerprint", "")
        duration = body.get("duration", "永久")
        # 计算过期时间
        if duration == "永久":
            expires = ""
        else:
            import re as _re
            m = _re.match(r'(\d+)(分钟|小时|天)', str(duration))
            if m:
                from datetime import datetime, timedelta
                num = int(m.group(1))
                unit = m.group(2)
                delta_map = {"分钟": timedelta(minutes=num), "小时": timedelta(hours=num), "天": timedelta(days=num)}
                expires = (datetime.now() + delta_map.get(unit, timedelta(hours=1))).strftime("%Y-%m-%d %H:%M:%S")
            else:
                expires = ""
        add_banned_user(name, reason, ip, uid, "admin", expires, fingerprint=fp)
        print(f"  [BAN] 手动封禁: {name} (reason={reason}, duration={duration})")
    elif action == "unban":
        from app.db import _get_conn
        conn = _get_conn()
        conn.execute("DELETE FROM banned_users WHERE name=?", (name,))
        conn.commit()
        print(f"  [BAN] 手动解封: {name}")
    else:
        return JSONResponse({"status": "error", "message": "未知操作"}, status_code=400)
    return {"status": "ok"}


@app.post("/api/banned/unban", dependencies=[Depends(verify_admin)])
async def unban_user(request: Request):
    """按ID解封"""
    from app.db import _get_conn
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    ban_id = body.get("id")
    conn = _get_conn()
    conn.execute("DELETE FROM banned_users WHERE id=?", (ban_id,))
    conn.commit()
    print(f"  [BAN] 解封 ban_id={ban_id}")
    return {"status": "ok"}


@app.get("/api/intros", dependencies=[Depends(verify_admin)])
async def list_intros():
    from app.db import get_intro_templates
    return {"templates": get_intro_templates(50)}


@app.post("/api/intros", dependencies=[Depends(verify_admin)])
async def edit_intro(request: Request):
    from app.db import add_intro_template, _get_conn
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    action = body.get("action", "")
    if action == "add":
        add_intro_template(body.get("content", "").strip())
    elif action == "delete":
        conn = _get_conn()
        conn.execute("DELETE FROM intro_templates WHERE content=?", (body.get("content", ""),))
        conn.commit()
    return {"status": "ok"}


# 全局公告
_current_announce = ""


@app.get("/api/announce")
async def get_announce():
    return {"content": _current_announce}


@app.post("/api/announce", dependencies=[Depends(verify_admin)])
async def set_announce(request: Request):
    global _current_announce
    try:
        body = await request.json()
        _current_announce = body.get("content", "").strip()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    # 广播给所有在线玩家
    from app.game.manager import game_manager
    for p in list(game_manager.players.values()):
        if not p.is_ai:
            try:
                await p.send_json({"type": "announce", "content": _current_announce or ""})
            except Exception:
                pass
    # 持久化到数据库
    try:
        from app.db import _get_conn
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('announcement', ?)",
            (_current_announce,),
        )
        conn.commit()
    except Exception:
        pass
    return {"status": "ok"}


@app.get("/api/ping")
async def ping():
    """健康检查"""
    return {"status": "ok", "time": __import__("time").time()}


@app.get("/api/chatlobby/search_users")
async def chatlobby_search_users(q: str = ""):
    """搜索注册用户（用于@提及）"""
    from app.db import _get_conn
    conn = _get_conn()
    if q:
        rows = conn.execute(
            "SELECT nickname, user_id FROM registered_nicknames WHERE nickname LIKE ? ORDER BY nickname LIMIT 500",
            (f"%{q}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT nickname, user_id FROM registered_nicknames ORDER BY nickname LIMIT 500").fetchall()
    # 标记是否在线
    from app.game.manager import game_manager
    online_uids = {p.user_id for p in game_manager.players.values() if p.user_id}
    # 也检查聊天大厅在线
    with _chat_lobby_lock:
        chat_uids = {u["user_id"] for u in _chat_lobby_users.values() if u["user_id"]}
    all_online = online_uids | chat_uids
    users = [{"nickname": r["nickname"], "user_id": r["user_id"], "is_online": r["user_id"] in all_online} for r in rows]
    return {"users": users}


@app.get("/api/online")
async def online_count():
    """返回当前在线人数"""
    from app.game.manager import game_manager
    ws_players = sum(1 for p in game_manager.players.values() if not p.is_ai)
    in_game = sum(1 for p in game_manager.players.values() if p.room_id and not p.is_ai)
    with _chat_lobby_lock:
        chat_lobby_count = len(_chat_lobby_users)
    return {"online": ws_players + chat_lobby_count, "in_game": in_game, "chat_lobby": chat_lobby_count}


@app.get("/api/admin/stats", dependencies=[Depends(_is_super_admin)])
async def get_stats():
    """超级管理员数据统计"""
    from app.db import _get_conn
    from app.game.manager import game_manager
    conn = _get_conn()
    total_users = conn.execute("SELECT count(*) FROM registered_nicknames").fetchone()[0]
    ws_players = sum(1 for p in game_manager.players.values() if not p.is_ai)
    in_game = sum(1 for p in game_manager.players.values() if p.room_id and not p.is_ai)
    with _chat_lobby_lock:
        chat_lobby_count = len(_chat_lobby_users)
    # 聊天大厅消息总数
    chat_msg_total = conn.execute("SELECT count(*) FROM chat大厅_messages").fetchone()[0]
    with _online_history_lock:
        history = list(_online_history)
    return {
        "online": ws_players + chat_lobby_count,
        "in_game": in_game,
        "chat_lobby": chat_lobby_count,
        "total_users": total_users,
        "chat_msg_total": chat_msg_total,
        "online_history": history,
    }


# ---- 聊天大厅管理 API ----
@app.get("/api/admin/chatlobby/stats", dependencies=[Depends(_is_super_admin)])
async def get_chat_lobby_stats():
    """聊天大厅管理数据"""
    from app.db import _get_conn
    conn = _get_conn()
    chat_msg_total = conn.execute("SELECT count(*) FROM chat大厅_messages").fetchone()[0]
    chat_non_recalled = conn.execute("SELECT count(*) FROM chat大厅_messages WHERE recalled=0").fetchone()[0]
    with _chat_lobby_lock:
        online_list = [{"user_id": u["user_id"], "nickname": u["nickname"]} for u in _chat_lobby_users.values()]
        online_count = len(online_list)
    return {
        "online_count": online_count,
        "online_list": online_list,
        "total_messages": chat_msg_total,
        "active_messages": chat_non_recalled,
    }


@app.post("/api/admin/chatlobby/clear_messages", dependencies=[Depends(_is_super_admin)])
async def clear_chat_lobby_messages(request: Request):
    """清空聊天大厅所有消息"""
    from app.db import _get_conn, log_admin_action
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = (body.get("reason") or "管理员清空").strip()
    conn = _get_conn()
    conn.execute("DELETE FROM chat大厅_messages")
    conn.execute("DELETE FROM chat_lobby_notifications")
    conn.commit()
    log_admin_action("root", "超管", "clear_chat_lobby", f"清空聊天大厅所有消息 理由:{reason}")
    # 广播清空
    await _chat_lobby_broadcast({"type": "messages_cleared", "message": f"聊天大厅消息已被管理员清空"})
    print(f"  [CHAT大厅] 聊天大厅消息已被管理员清空")
    return {"status": "ok", "message": "聊天大厅消息已清空"}


@app.post("/api/admin/chatlobby/recall_message", dependencies=[Depends(_is_super_admin)])
async def admin_recall_chatlobby_message(request: Request):
    """管理员撤回聊天大厅指定消息"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    msg_id = (body.get("msg_id") or "").strip()
    admin_name = (body.get("admin_name") or "管理员").strip()
    
    if not msg_id:
        raise HTTPException(status_code=400, detail="必须提供 msg_id")
    
    from app.db import chat_recall_message, chat_get_message_by_id, log_admin_action
    # 先获取消息信息，用于广播
    original_msg = chat_get_message_by_id(msg_id)
    result = chat_recall_message(msg_id, user_id="", force_admin=True)
    
    if not result:
        raise HTTPException(status_code=404, detail="消息不存在或已被撤回")
    
    # 广播撤回通知给所有聊天大厅在线用户
    original_nick = original_msg.get("nickname", "") if original_msg else ""
    await _chat_lobby_broadcast({
        "type": "admin_recalled",
        "msg_id": msg_id,
        "original_nickname": original_nick
    })
    log_admin_action("root", admin_name, "recall_message", f"撤回消息 {msg_id} (发送者:{original_nick})")
    print(f"  [CHAT大厅] 管理员 {admin_name} 撤回了消息 {msg_id}")
    return {"status": "ok", "message": f"消息 {msg_id} 已被管理员撤回"}


@app.get("/api/admin/chatlobby/recent_messages", dependencies=[Depends(_is_super_admin)])
async def admin_get_chatlobby_messages(limit: int = 50):
    """管理员获取聊天大厅最近消息（含msg_id用于撤回操作）"""
    from app.db import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        "SELECT msg_id, nickname, content, recalled, created_at "
        "FROM chat大厅_messages ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return {
        "messages": [
            {
                "msg_id": row[0],
                "nickname": row[1],
                "content": row[2],
                "recalled": bool(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]
    }


@app.post("/api/admin/chatlobby/send_warning", dependencies=[Depends(_is_super_admin)])
async def send_chat_lobby_warning(request: Request):
    """管理员全局警告"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效JSON"}, status_code=400)
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"status": "error", "message": "警告内容不能为空"}, status_code=400)
    await _chat_lobby_broadcast({"type": "warning", "message": message, "from": "管理员"})
    # 同时广播到游戏WS玩家
    from app.game.manager import game_manager
    for p in list(game_manager.players.values()):
        if not p.is_ai:
            try:
                await p.send_json({"type": "warning", "content": message, "from": "管理员"})
            except Exception:
                pass
    return {"status": "ok", "message": "警告已发送"}


@app.post("/api/admin/chatlobby/kick_user", dependencies=[Depends(_is_super_admin)])
async def kick_chat_lobby_user(request: Request):
    """管理员踢出聊天大厅用户"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效JSON"}, status_code=400)
    target_uid = (body.get("user_id") or "").strip()
    if not target_uid:
        return JSONResponse({"status": "error", "message": "未指定用户"}, status_code=400)
    with _chat_lobby_lock:
        for ws_id, u in list(_chat_lobby_users.items()):
            if u["user_id"] == target_uid:
                try:
                    await u["websocket"].send_json({"type": "kicked", "message": "你已被管理员踢出聊天大厅"})
                    await u["websocket"].close()
                except Exception:
                    pass
                _chat_lobby_users.pop(ws_id, None)
                await _chat_lobby_broadcast({
                    "type": "user_leave", "nickname": u["nickname"],
                    "online_count": len(_chat_lobby_users),
                })
                return {"status": "ok", "message": f"已踢出 {u['nickname']}"}
    return {"status": "error", "message": "用户不在线"}


@app.post("/api/admin/maintenance", dependencies=[Depends(_is_super_admin)])
async def set_maintenance(request: Request):
    """设置维护模式开关"""
    from app.config import GAME_CONFIG, save_config
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效 JSON"}, status_code=400)
    enabled = bool(body.get("enabled", False))
    msg = (body.get("message") or "系统维护中，请稍后再试").strip()
    GAME_CONFIG["maintenance_mode"] = enabled
    GAME_CONFIG["maintenance_message"] = msg
    save_config(GAME_CONFIG)
    # 广播维护模式状态给所有在线玩家
    from app.game.manager import game_manager
    for p in list(game_manager.players.values()):
        if not p.is_ai:
            try:
                await p.send_json({"type": "maintenance", "enabled": enabled, "message": msg})
            except Exception:
                pass
    return {"status": "ok", "maintenance_mode": enabled}


@app.get("/api/maintenance")
async def check_maintenance():
    """前端检查维护模式"""
    from app.config import GAME_CONFIG
    return {"enabled": GAME_CONFIG.get("maintenance_mode", False), "message": GAME_CONFIG.get("maintenance_message", "")}


@app.post("/api/global-learn", dependencies=[Depends(verify_admin)])
async def global_learn():
    """触发全局学习（从所有对话中学习聊天习惯）"""
    from app.ai.global_learner import train_global_knowledge, get_global_knowledge
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, train_global_knowledge)
    return {"status": "ok", "knowledge": {k: len(v) for k, v in result.items()}}


@app.get("/api/prompt-debug", dependencies=[Depends(verify_admin)])
async def prompt_debug():
    from app.game.ai_player import get_prompt_debug_log
    return {"logs": get_prompt_debug_log()}


@app.get("/api/memory", dependencies=[Depends(verify_admin)])
async def get_memory():
    """返回所有会话的长期记忆（跨管理面板 + 游戏房间）"""
    from app.db import get_all_learned_styles, get_all_learned_jargon, get_all_conversations

    styles = [{"situation": s, "style": t, "sid": sid} for s, t, sid in get_all_learned_styles()]
    jargon = [{"word": w, "sid": sid} for w, sid in get_all_learned_jargon()]
    history = [{"role": r, "content": c, "sid": sid} for r, c, sid in get_all_conversations(30)]
    return {"styles": styles, "jargon": jargon, "history": history}


@app.post("/api/memory", dependencies=[Depends(verify_admin)])
async def edit_memory(request: Request):
    """增删长期记忆"""
    from app.db import _get_conn
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)

    action = body.get("action", "")
    conn = _get_conn()

    if action == "delete_style":
        conn.execute(
            "DELETE FROM learned_styles WHERE situation=? AND style=?",
            (body.get("situation", ""), body.get("style", "")),
        )
    elif action == "delete_jargon":
        conn.execute(
            "DELETE FROM learned_jargon WHERE word=?",
            (body.get("word", ""),),
        )
    elif action == "add_style":
        conn.execute(
            "INSERT INTO learned_styles (session_id, situation, style) VALUES (?,?,?)",
            ("admin", body.get("situation", ""), body.get("style", "")),
        )
    elif action == "update_style":
        conn.execute(
            "UPDATE learned_styles SET situation=?, style=? WHERE situation=? AND style=?",
            (body.get("new_situation", ""), body.get("new_style", ""),
             body.get("old_situation", ""), body.get("old_style", "")),
        )
    elif action == "add_jargon":
        conn.execute(
            "INSERT INTO learned_jargon (session_id, word) VALUES (?,?)",
            ("admin", body.get("word", "")),
        )
    elif action == "update_jargon":
        conn.execute(
            "UPDATE learned_jargon SET word=? WHERE word=?",
            (body.get("new_word", ""), body.get("old_word", "")),
        )
    else:
        return JSONResponse({"status": "error", "message": "未知操作"}, status_code=400)

    conn.commit()
    return {"status": "ok"}


@app.post("/api/models", dependencies=[Depends(verify_admin)])
async def list_models(request: Request):
    """列出接口支持的模型"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效的 JSON"}, status_code=400)
    base_url = str(body.get("base_url", "")).rstrip("/")
    api_key = body.get("api_key", "")
    if not base_url:
        return JSONResponse({"status": "error", "message": "请先填写接口地址"}, status_code=400)
    try:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        return {"status": "ok", "models": models}
    except Exception as e:
        return JSONResponse({"status": "error", "message": f"获取模型失败: {e}"}, status_code=500)


# ---- 用户系统 ----


@app.post("/api/register_nickname")
async def register_nickname(request: Request):
    """注册/更新昵称，检查是否已被占用"""
    from app.db import register_nickname as _reg, is_nickname_taken
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效的 JSON"}, status_code=400)
    nickname = (body.get("nickname") or "").strip()
    user_id = (body.get("user_id") or "").strip()
    ip = request.client.host if request.client else ""
    if not nickname or not user_id:
        return JSONResponse({"status": "error", "message": "昵称和用户 ID 不能为空"}, status_code=400)
    if len(nickname) > 20:
        return JSONResponse({"status": "error", "message": "昵称过长"}, status_code=400)
    # 检查是否被其他用户占用
    if is_nickname_taken(nickname, exclude_user_id=user_id):
        return JSONResponse({"status": "error", "message": "该昵称已被其他用户使用"}, status_code=409)
    _reg(nickname, user_id, ip)
    return {"status": "ok", "nickname": nickname, "user_id": user_id}


@app.post("/api/check_nickname")
async def check_nickname(request: Request):
    """检查昵称是否可用"""
    from app.db import is_nickname_taken as _check
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    nickname = (body.get("nickname") or "").strip()
    user_id = (body.get("user_id") or "").strip()
    if not nickname:
        return JSONResponse({"status": "error", "message": "昵称不能为空"}, status_code=400)
    taken = _check(nickname, exclude_user_id=user_id)
    return {"status": "ok", "taken": taken}


@app.post("/api/change_nickname")
async def change_nickname_api(request: Request):
    """修改用户昵称（30天冷却）"""
    from app.db import change_nickname as _change_nick
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "无效的 JSON"}, status_code=400)
    user_id = (body.get("user_id") or "").strip()
    old_nickname = (body.get("old_nickname") or "").strip()
    new_nickname = (body.get("new_nickname") or "").strip()
    if not user_id or not new_nickname:
        return JSONResponse({"status": "error", "message": "参数不完整"}, status_code=400)
    if len(new_nickname) > 20:
        return JSONResponse({"status": "error", "message": "昵称过长（最多20字）"}, status_code=400)
    ip = request.client.host if request.client else ""
    success, msg = _change_nick(user_id, old_nickname, new_nickname, ip)
    if success:
        # 同步更新在线玩家昵称
        from app.game.manager import game_manager
        for p in game_manager.players.values():
            if p.user_id == user_id:
                p.name = new_nickname
                break
        return {"status": "ok", "message": msg, "new_nickname": new_nickname}
    else:
        return JSONResponse({"status": "error", "message": msg}, status_code=400)


@app.post("/api/admin/login_log", dependencies=[Depends(_is_any_admin)])
async def record_admin_login(request: Request):
    """记录管理员登录信息"""
    from app.db import log_admin_login
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    username = (body.get("username") or "").strip()
    user_id = (body.get("user_id") or "").strip()
    nickname = (body.get("nickname") or "").strip()
    fingerprint = (body.get("fingerprint") or "").strip()
    ip = request.client.host if request.client else ""
    log_admin_login(username, user_id, nickname, ip, fingerprint)
    return {"status": "ok"}


@app.get("/api/admin/login_logs", dependencies=[Depends(_is_super_admin)])
async def get_admin_login_logs():
    from app.db import get_admin_login_logs
    return {"logs": get_admin_login_logs(100)}


@app.post("/api/admin/verify_password")
async def verify_admin_password(request: Request):
    """验证管理员密码（超管或小管理员）"""
    from app.db import get_admin_account
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    password = (body.get("password") or "").strip()
    user_id = (body.get("user_id") or "").strip()
    nickname = (body.get("nickname") or "").strip()
    fingerprint = (body.get("fingerprint") or "").strip()
    ip = request.client.host if request.client else ""

    # 验证超管密码
    if password == SUPER_ADMIN_PASSWORD:
        # 记录登录
        from app.db import log_admin_login
        log_admin_login("root", user_id, nickname, ip, fingerprint)
        return {"status": "ok", "role": "super_admin", "token": SUPER_ADMIN_HASH}

    # 验证小管理员密码：遍历所有管理员，用 SHA256 对比
    try:
        hashed_pw = _hash_admin_pw(password)
        from app.db import list_admin_accounts
        for a in list_admin_accounts():
            if a.get("password_hash") and a["password_hash"] == hashed_pw:
                if a.get("is_active"):
                    from app.db import log_admin_login
                    log_admin_login(a["username"], user_id, nickname, ip, fingerprint)
                    return {"status": "ok", "role": "admin", "token": hashed_pw}
    except Exception:
        pass

    return JSONResponse({"status": "error", "message": "密码错误"}, status_code=403)


@app.get("/api/admin/admins", dependencies=[Depends(_is_super_admin)])
async def list_admins():
    from app.db import list_admin_accounts
    return {"admins": list_admin_accounts()}


@app.post("/api/admin/admins/create", dependencies=[Depends(_is_super_admin)])
async def create_admin(request: Request):
    """创建小管理员"""
    from app.db import create_admin_account
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    username = (body.get("username") or "").strip()
    if not username:
        return JSONResponse({"status": "error", "message": "请输入管理员名称"}, status_code=400)
    # 生成随机密码
    rand_pw = secrets.token_hex(8)
    pw_hash = _hash_admin_pw(rand_pw)
    create_admin_account(username, pw_hash, role="admin", created_by="root")
    return {"status": "ok", "username": username, "password": rand_pw}


@app.post("/api/admin/admins/delete", dependencies=[Depends(_is_super_admin)])
async def delete_admin(request: Request):
    from app.db import delete_admin_account
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    username = (body.get("username") or "").strip()
    if not username or username == "root":
        return JSONResponse({"status": "error", "message": "无法删除超管"}, status_code=400)
    delete_admin_account(username)
    return {"status": "ok"}


@app.get("/api/admin/users")
async def search_users(request: Request):
    """用户搜索（需要管理员权限）。q为空时返回所有注册用户"""
    try:
        _is_any_admin(request)
    except HTTPException:
        token = request.cookies.get("admin_token", "")
        if token != SUPER_ADMIN_HASH:
            found = False
            try:
                from app.db import list_admin_accounts
                admins = list_admin_accounts()
                for a in admins:
                    if a.get("password_hash") and a["password_hash"] == token:
                        found = True
                        break
            except Exception:
                pass
            if not found:
                return JSONResponse({"status": "error", "message": "需要管理员权限"}, status_code=403)

    from app.db import search_users as _search, get_all_users, get_all_active_players
    query = request.query_params.get("q", "").strip()

    # 获取用户列表
    all_users = get_all_users() if not query else _search(query)

    # 获取在线用户列表，用于标记
    online_map = {}
    for p in get_all_active_players():
        key = p.get("id") or p.get("user_id") or p.get("name")
        if key:
            online_map[key] = True
            if p.get("name"): online_map[p["name"]] = True
            if p.get("ip"): online_map[p["ip"]] = True
            if p.get("user_id"): online_map[p["user_id"]] = True

    # 标记在线状态
    for u in all_users:
        uid = u.get("user_id", "")
        name = u.get("nickname", "")
        ip = u.get("ip", "")
        u["is_online"] = bool(online_map.get(uid) or online_map.get(name) or online_map.get(ip))

    return {"users": all_users, "online": get_all_active_players()}


@app.post("/api/admin/warn")
async def warn_user(request: Request):
    """警告用户（需要管理员权限）"""
    try:
        _is_any_admin(request)
    except HTTPException:
        return JSONResponse({"status": "error", "message": "需要管理员权限"}, status_code=403)

    from app.db import add_warning
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    target_user_id = (body.get("user_id") or "").strip()
    message = (body.get("message") or "").strip()
    target_name = (body.get("name") or "").strip()
    target_ip = (body.get("ip") or "").strip()
    if not target_user_id or not message:
        return JSONResponse({"status": "error", "message": "参数不全"}, status_code=400)
    add_warning(target_user_id, message, issued_by="admin", target_name=target_name, target_ip=target_ip)

    # 格式化警告消息（与对局中 watch_warn 保持一致的前缀风格）
    warn_msg = f"[管理员警告] {message}"
    warned = False

    # 如果用户在游戏WebSocket中，推送警告
    from app.game.manager import game_manager
    for pid, p in game_manager.players.items():
        if p.user_id == target_user_id:
            try:
                await p.send_json({"type": "watch_warn", "message": warn_msg, "from_admin": True})
                warned = True
            except Exception:
                pass

    # 如果用户在聊天大厅WebSocket中，也推送警告
    with _chat_lobby_lock:
        for u in _chat_lobby_users.values():
            if u.get("user_id") == target_user_id or u.get("nickname") == target_name:
                try:
                    await u["websocket"].send_json({
                        "type": "watch_warn",
                        "message": warn_msg,
                        "from_admin": True
                    })
                    warned = True
                except Exception:
                    pass
                break

    if warned:
        return {"status": "ok", "message": "警告已推送给在线用户"}
    return {"status": "ok", "message": "警告已记录到数据库（用户不在线）"}


@app.get("/api/admin/warnings")
async def get_warnings(request: Request):
    """获取警告列表（管理员）"""
    try:
        _is_any_admin(request)
    except HTTPException:
        return JSONResponse({"status": "error", "message": "需要管理员权限"}, status_code=403)
    from app.db import get_all_warnings
    return {"warnings": get_all_warnings(100)}


@app.post("/api/admin/warnings/delete")
async def delete_warning(request: Request):
    try:
        _is_any_admin(request)
    except HTTPException:
        return JSONResponse({"status": "error"}, status_code=403)
    from app.db import delete_warning as _del
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    _del(body.get("id", 0))
    return {"status": "ok"}


@app.post("/api/user/pending_warnings")
async def get_pending_warnings(request: Request):
    """获取用户的待处理警告"""
    from app.db import get_pending_warnings, mark_warnings_read
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    user_id = (body.get("user_id") or "").strip()
    if not user_id:
        return JSONResponse({"status": "error", "message": "需要 user_id"}, status_code=400)
    warnings = get_pending_warnings(user_id)
    mark_warnings_read(user_id)
    return {"warnings": warnings}


# ---- 贴纸管理 ----
STICKERS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "stickers")
os.makedirs(STICKERS_DIR, exist_ok=True)


@app.get("/api/stickers")
async def list_stickers():
    from app.db import get_stickers
    stickers = get_stickers()
    return {"stickers": stickers}


@app.post("/api/stickers", dependencies=[Depends(verify_admin)])
async def upload_sticker(file: UploadFile = File(...), description: str = "", category: str = ""):
    from app.db import add_sticker
    if not file.filename:
        return JSONResponse({"status": "error", "message": "无文件"}, status_code=400)
    # 安全文件名
    safe_name = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
    import uuid as _uuid
    filename = f"{_uuid.uuid4().hex[:8]}.{safe_name}"
    filepath = os.path.join(STICKERS_DIR, filename)
    content = await file.read()
    with open(filepath, "wb") as f:
        f.write(content)
    add_sticker(filename, description.strip(), category.strip())
    return {"status": "ok", "filename": filename}


@app.post("/api/stickers/delete", dependencies=[Depends(verify_admin)])
async def delete_sticker_api(request: Request):
    from app.db import delete_sticker, get_sticker_by_filename
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "error"}, status_code=400)
    sticker_id = body.get("id")
    if not sticker_id:
        return JSONResponse({"status": "error", "message": "需要 id"}, status_code=400)
    # 删除文件
    from app.db import _get_conn
    conn = _get_conn()
    row = conn.execute("SELECT filename FROM stickers WHERE id=?", (sticker_id,)).fetchone()
    if row:
        filepath = os.path.join(STICKERS_DIR, row["filename"])
        if os.path.exists(filepath):
            os.remove(filepath)
    delete_sticker(sticker_id)
    return {"status": "ok"}


# ---- WebSocket ----
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else "0.0.0.0"
    player = Player(websocket, client_ip)

    # 维护模式检查
    from app.config import GAME_CONFIG
    if GAME_CONFIG.get("maintenance_mode"):
        try:
            await player.send_json({"type": "maintenance", "enabled": True, "message": GAME_CONFIG.get("maintenance_message", "系统维护中")})
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
        return

    try:
        await asyncio.wait_for(
            handle_player(websocket, player),
            timeout=1800,  # 30 分钟自动断开
        )
    except asyncio.TimeoutError:
        print(f"  [WS] 玩家 {player.id} 连接超时，自动断开")
        # 超时时 handle_player 的 finally 不会执行，需在此清理
        from app.game.manager import game_manager
        from app.game.multiplayer import handle_multi_leave as _handle_multi_leave
        try:
            await _handle_multi_leave(player)
        except Exception:
            pass
        game_manager.unregister_player(player.id)
    except Exception as e:
        if "accept" not in str(e).lower():
            print(f"[WS] 错误 player={player.id}: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---- 实时控制台日志 WebSocket ----
import sys
import io

class _TeeWriter:
    """拦截 print/sys.stdout 输出，同时转发给连接的管理员 WebSocket"""
    def __init__(self, original):
        self._original = original
        self._subscribers: list = []

    def write(self, text):
        self._original.write(text)
        if text.strip() and self._subscribers:
            import json as _json
            msg = _json.dumps({"type": "console_log", "text": text}, ensure_ascii=False)
            dead = []
            for ws in self._subscribers:
                try:
                    import asyncio
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None
                    if loop and loop.is_running():
                        asyncio.ensure_future(ws.send_text(msg))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                try:
                    self._subscribers.remove(ws)
                except ValueError:
                    pass

    def flush(self):
        self._original.flush()

_console_ws_subscribers: list[WebSocket] = []
_tee = None

def _install_tee():
    global _tee
    if _tee is None:
        _tee = _TeeWriter(sys.stdout)
        sys.stdout = _tee
        _tee_err = _TeeWriter(sys.stderr)
        sys.stderr = _tee_err

@app.websocket("/ws/console")
async def ws_console(websocket: WebSocket):
    await websocket.accept()
    # 验证 token: 从 query 参数或 cookie 中获取
    token = websocket.query_params.get("token", "")
    if not token:
        token = websocket.cookies.get(TOKEN_COOKIE, "")
    from app.db import list_admin_accounts
    super_hash = SUPER_ADMIN_HASH
    is_valid = (token == super_hash)
    if not is_valid and token:
        # 也检查普通管理员（用 password_hash 匹配）
        try:
            accounts = list_admin_accounts()
            for a in accounts:
                if a.get("password_hash") and a["password_hash"] == token:
                    is_valid = True
                    break
        except Exception:
            pass
    if not is_valid:
        try:
            await websocket.send_json({"type": "error", "message": "无权限"})
            await websocket.close()
        except Exception:
            pass
        return

    _install_tee()
    _tee._subscribers.append(websocket)
    try:
        await websocket.send_json({"type": "console_log", "text": "[LOG] 已连接到实时日志流"})
        while True:
            data = await websocket.receive_text()
            # 客户端可以发 ping
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        if websocket in _tee._subscribers:
            _tee._subscribers.remove(websocket)


# ---- 聊天大厅 WebSocket ----
import threading as _chat_threading
_chat_lobby_users = {}  # ws_id -> {"websocket": ws, "user_id": uid, "nickname": nick, "ip": ip, "is_admin": bool}
_chat_lobby_lock = _chat_threading.Lock()

async def _chat_lobby_broadcast(data: dict, exclude=None):
    """广播给所有聊天大厅在线用户（带超时，防止慢客户端卡住广播）"""
    with _chat_lobby_lock:
        targets = list(_chat_lobby_users.values())
    for u in targets:
        if u["websocket"] is not exclude:
            try:
                await asyncio.wait_for(u["websocket"].send_json(data), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

def _chat_lobby_online_list():
    with _chat_lobby_lock:
        return [{"user_id": u["user_id"], "nickname": u["nickname"], "is_admin": u["is_admin"]} for u in _chat_lobby_users.values()]

@app.websocket("/ws/chat大厅")
async def ws_chat_lobby(websocket: WebSocket):
    await websocket.accept()
    client_ip = websocket.client.host if websocket.client else "0.0.0.0"
    ws_id = id(websocket)
    user_info = {"websocket": websocket, "user_id": "", "nickname": "", "ip": client_ip, "is_admin": False}

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type", "")

            if msg_type == "join":
                user_id = (data.get("user_id") or "").strip()
                nickname = (data.get("nickname") or "").strip() or f"游客{random.randint(1000,9999)}"
                # 检查封禁 - 允许进入但标记为禁言
                from app.db import is_banned
                is_banned_user = is_banned(user_id=user_id, name=nickname, ip=client_ip)
                user_info["user_id"] = user_id
                user_info["nickname"] = nickname
                user_info["is_muted"] = is_banned_user
                # 检查是否超管
                if data.get("admin_token") == SUPER_ADMIN_HASH:
                    user_info["is_admin"] = True
                with _chat_lobby_lock:
                    _chat_lobby_users[ws_id] = user_info
                online = _chat_lobby_online_list()
                await websocket.send_json({"type": "joined", "online_count": len(online), "online_list": online})
                # 广播上线
                await _chat_lobby_broadcast({"type": "user_join", "nickname": nickname, "online_count": len(online)}, exclude=websocket)
                print(f"  [CHAT大厅] {nickname} 进入聊天大厅 ({len(online)}人在线)")
                # 禁言提示
                if is_banned_user:
                    await websocket.send_json({"type": "muted", "message": "你已被禁言，无法发送消息"})
                # 推送离线通知
                if user_id:
                    from app.db import chat_get_pending_notifications, chat_mark_notifications_delivered
                    pending = chat_get_pending_notifications(user_id)
                    if pending:
                        for n in pending:
                            noti_data = {
                                "type": "at_mention" if n["noti_type"] == "at" else "reply_notify",
                                "from": n["from_nickname"],
                                "msg_id": n["msg_id"],
                                "content": n.get("message_content", ""),
                            }
                            try:
                                await websocket.send_json(noti_data)
                            except Exception:
                                pass
                        chat_mark_notifications_delivered(user_id)

            elif msg_type == "chat":
                if not user_info["user_id"]:
                    await websocket.send_json({"type": "error", "message": "请先加入"})
                    continue
                # 检查禁言
                from app.db import is_banned
                if user_info.get("is_muted") or is_banned(user_id=user_info["user_id"], name=user_info["nickname"]):
                    await websocket.send_json({"type": "muted", "message": "你已被禁言，无法发送消息"})
                    continue
                content = (data.get("content") or "").strip()
                if not content or len(content) > 200:
                    continue
                msg_id = data.get("msg_id") or f"msg_{uuid.uuid4().hex[:12]}"
                reply_to = (data.get("reply_to") or "").strip()
                # 保存消息
                from app.db import chat_save_message
                chat_save_message(msg_id, user_info["user_id"], user_info["nickname"], content, "text", reply_to)
                # 获取被回复消息的内容
                reply_content = ""
                reply_sender = ""
                if reply_to:
                    from app.db import chat_get_message_by_id
                    replied = chat_get_message_by_id(reply_to)
                    if replied:
                        reply_content = replied.get("content", "")
                        reply_sender = replied.get("nickname", "")
                # 广播
                msg_data = {
                    "type": "chat", "msg_id": msg_id, "user_id": user_info["user_id"],
                    "nickname": user_info["nickname"], "content": content,
                    "reply_to": reply_to, "reply_content": reply_content, "reply_sender": reply_sender,
                    "created_at": __import__("datetime").datetime.now().strftime("%H:%M:%S"),
                }
                await _chat_lobby_broadcast(msg_data)
                # @提及通知
                import re as _chat_re
                at_matches = _chat_re.findall(r'@(\S+)', content)
                if at_matches:
                    with _chat_lobby_lock:
                        online_users = list(_chat_lobby_users.values())
                    # 收集所有@目标的user_id
                    at_target_uids = []
                    for u in online_users:
                        if u["user_id"] != user_info["user_id"] and any(at in u["nickname"] for at in at_matches):
                            try:
                                await u["websocket"].send_json({
                                    "type": "at_mention", "from": user_info["nickname"],
                                    "msg_id": msg_id, "content": content,
                                })
                                at_target_uids.append(u["user_id"])
                            except Exception:
                                pass
                    # 离线的@目标存通知
                    from app.db import chat_save_notification, _get_conn
                    conn_db = _get_conn()
                    all_at_users = conn_db.execute(
                        "SELECT nickname, user_id FROM registered_nicknames WHERE " + " OR ".join(["nickname LIKE ?" for _ in at_matches]),
                        tuple(f"%{at}%" for at in at_matches),
                    ).fetchall()
                    for u_row in all_at_users:
                        uid = u_row["user_id"]
                        if uid and uid != user_info["user_id"] and uid not in at_target_uids:
                            chat_save_notification(uid, user_info["nickname"], "at", msg_id, content)
                # 回复通知
                if reply_to:
                    from app.db import chat_get_message_by_id, chat_save_notification
                    replied_msg = chat_get_message_by_id(reply_to)
                    if replied_msg and replied_msg["user_id"] != user_info["user_id"]:
                        target_uid = replied_msg["user_id"]
                        target = None
                        with _chat_lobby_lock:
                            for u in _chat_lobby_users.values():
                                if u["user_id"] == target_uid:
                                    target = u
                                    break
                        if target:
                            try:
                                await target["websocket"].send_json({
                                    "type": "reply_notify", "from": user_info["nickname"],
                                    "msg_id": msg_id, "reply_to": reply_to,
                                })
                            except Exception:
                                pass
                        else:
                            # 离线，存通知
                            chat_save_notification(target_uid, user_info["nickname"], "reply", msg_id, content)

            elif msg_type == "sticker":
                if not user_info["user_id"]:
                    continue
                # 检查禁言
                from app.db import is_banned
                if user_info.get("is_muted") or is_banned(user_id=user_info["user_id"], name=user_info["nickname"]):
                    await websocket.send_json({"type": "muted", "message": "你已被禁言，无法发送消息"})
                    continue
                filename = (data.get("filename") or "").strip()
                if not filename:
                    continue
                msg_id = data.get("msg_id") or f"msg_{uuid.uuid4().hex[:12]}"
                reply_to = (data.get("reply_to") or "").strip()
                reply_content = ""
                reply_sender = ""
                if reply_to:
                    from app.db import chat_get_message_by_id
                    replied = chat_get_message_by_id(reply_to)
                    if replied:
                        reply_content = replied.get("content", "")
                        reply_sender = replied.get("nickname", "")
                from app.db import chat_save_message
                chat_save_message(msg_id, user_info["user_id"], user_info["nickname"], filename, "sticker", reply_to)
                await _chat_lobby_broadcast({
                    "type": "chat", "msg_id": msg_id, "user_id": user_info["user_id"],
                    "nickname": user_info["nickname"], "content": filename,
                    "msg_type": "sticker", "reply_to": reply_to,
                    "reply_content": reply_content, "reply_sender": reply_sender,
                    "created_at": __import__("datetime").datetime.now().strftime("%H:%M:%S"),
                })

            elif msg_type == "recall":
                msg_id = (data.get("msg_id") or "").strip()
                if msg_id and user_info["user_id"]:
                    from app.db import chat_recall_message, chat_get_message_by_id
                    is_admin = user_info.get("is_admin", False)
                    # 先获取消息，检查是否为管理员撤回他人消息
                    original_msg = chat_get_message_by_id(msg_id)
                    is_admin_recall_other = is_admin and original_msg and original_msg.get("user_id") != user_info["user_id"]
                    if chat_recall_message(msg_id, user_info["user_id"], force_admin=is_admin):
                        if is_admin_recall_other:
                            original_nick = original_msg.get("nickname", "")
                            await _chat_lobby_broadcast({"type": "admin_recalled", "msg_id": msg_id, "original_nickname": original_nick})
                        else:
                            await _chat_lobby_broadcast({"type": "recalled", "msg_id": msg_id, "nickname": user_info["nickname"]})

            elif msg_type == "report":
                if not user_info["user_id"]:
                    continue
                target_msg_id = (data.get("msg_id") or "").strip()
                reason = (data.get("reason") or "").strip()
                if target_msg_id and reason:
                    from app.db import save_report, chat_get_message_by_id
                    # 获取被举报消息的发送者作为offender
                    msg_info = chat_get_message_by_id(target_msg_id)
                    offender = msg_info.get("nickname", "未知") if msg_info else "未知"
                    save_report(
                        session_id="chat大厅",
                        reporter=user_info["nickname"],
                        offender=offender,
                        reason=reason,
                        trigger_msg=target_msg_id,
                        full_log=msg_info.get("content", "") if msg_info else "",
                        reporter_ip=client_ip,
                    )
                    await websocket.send_json({"type": "report_ok", "message": "举报已提交"})

            elif msg_type == "warn":
                # 管理员发送全局警告
                if not user_info["is_admin"]:
                    continue
                warn_msg = (data.get("message") or "").strip()
                if warn_msg:
                    await _chat_lobby_broadcast({"type": "warning", "message": warn_msg, "from": user_info["nickname"]})
                    # 同时广播到游戏WS玩家
                    from app.game.manager import game_manager
                    for p in list(game_manager.players.values()):
                        if not p.is_ai:
                            try:
                                await p.send_json({"type": "warning", "content": warn_msg, "from": user_info["nickname"]})
                            except Exception:
                                pass

            elif msg_type == "ban_user":
                # 管理员封禁用户
                if not user_info["is_admin"]:
                    continue
                target_uid = (data.get("target_user_id") or "").strip()
                target_nick = (data.get("target_nickname") or "").strip()
                reason = (data.get("reason") or "管理员封禁").strip()
                duration = (data.get("duration") or "1天").strip()
                if target_uid or target_nick:
                    from app.db import add_banned_user, log_admin_action
                    import re as _re
                    from datetime import datetime as _dt, timedelta as _td
                    # 获取被封禁用户的IP
                    target_ip = ""
                    with _chat_lobby_lock:
                        for u in _chat_lobby_users.values():
                            if u["user_id"] == target_uid:
                                target_ip = u.get("ip", "")
                                break
                    # 解析封禁时长（与对局中 watch_ban 逻辑一致）
                    expires_at = ""
                    if duration and duration not in ("永久", "permanent", "forever"):
                        m = _re.match(r'(\d+)(分钟|小时|天)', str(duration))
                        if m:
                            num = int(m.group(1))
                            unit = m.group(2)
                            delta_map = {"分钟": _td(minutes=num), "小时": _td(hours=num), "天": _td(days=num)}
                            expires_at = (_dt.now() + delta_map.get(unit, _td(hours=1))).strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            expires_at = (_dt.now() + _td(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
                    add_banned_user(name=target_nick, user_id=target_uid, ip=target_ip, reason=reason, banned_by=user_info["nickname"], expires_at=expires_at)
                    log_admin_action(user_info["user_id"], user_info["nickname"], "chat_ban", f"封禁 {target_nick}({target_uid}) {duration} 到期:{expires_at or '永久'} 理由:{reason}")
                    # 标记被封禁用户为禁言状态（不踢出）
                    banned_ws = None
                    with _chat_lobby_lock:
                        for u in list(_chat_lobby_users.values()):
                            if u["user_id"] == target_uid:
                                u["is_muted"] = True
                                banned_ws = u["websocket"]
                                break
                    if banned_ws:
                        try:
                            await banned_ws.send_json({"type": "muted", "message": f"你已被管理员禁言: {reason}"})
                        except Exception:
                            pass
                    # 广播封禁通知
                    await _chat_lobby_broadcast({"type": "user_muted", "nickname": target_nick, "reason": reason, "from": user_info["nickname"]})
                    await websocket.send_json({"type": "ban_ok", "message": f"已封禁 {target_nick}（禁言 + 禁止匹配）"})

            elif msg_type == "get_history":
                before_id = (data.get("before_id") or "").strip()
                limit = min(int(data.get("limit") or 50), 200)
                from app.db import chat_get_history, chat_get_message_by_id
                history = chat_get_history(limit, before_id)
                # 为每条历史消息补上回复信息
                for msg in history:
                    if msg.get("reply_to"):
                        replied = chat_get_message_by_id(msg["reply_to"])
                        if replied:
                            msg["reply_content"] = replied.get("content", "")
                            msg["reply_sender"] = replied.get("nickname", "")
                await websocket.send_json({"type": "history", "messages": history})

            elif msg_type == "get_online":
                online = _chat_lobby_online_list()
                await websocket.send_json({"type": "online_list", "online_count": len(online), "online_list": online})

            elif msg_type == "ping":
                await websocket.send_json({"type": "pong", "t": data.get("t", 0)})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        if "accept" not in str(e).lower():
            print(f"  [CHAT大厅] 错误: {e}")
    finally:
        with _chat_lobby_lock:
            _chat_lobby_users.pop(ws_id, None)
        if user_info["nickname"]:
            online = _chat_lobby_online_list()
            await _chat_lobby_broadcast({"type": "user_leave", "nickname": user_info["nickname"], "online_count": len(online)})
            print(f"  [CHAT大厅] {user_info['nickname']} 离开 ({len(online)}人在线)")


def run(host: str = "", port: int = 0):
    import uvicorn
    from app.ai.llm import _close_client, _refresh_key_cycle

    # 从 AIkey.txt 加载额外密钥到密钥池
    try:
        aikey_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "AIkey.txt")
        if os.path.exists(aikey_path):
            with open(aikey_path, "r", encoding="utf-8") as f:
                file_keys = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            if file_keys:
                existing_keys = [k.strip() for k in AI_CONFIG.get("api_key", "").split(",") if k.strip()]
                all_keys = list(dict.fromkeys(existing_keys + file_keys))  # 去重保序
                AI_CONFIG["api_key"] = ",".join(all_keys)
                print(f"  从 AIkey.txt 加载了 {len(file_keys)} 个密钥，当前共 {len(all_keys)} 个密钥")
    except Exception as e:
        print(f"  加载 AIkey.txt 失败: {e}")

    # 刷新 API Key 列表
    _refresh_key_cycle()

    print("=" * 50)
    print("  图灵测试 · Turing Test")
    print(f"  http://{host}:{port}")
    print(f"  管理面板: http://{host}:{port}/admin")
    print("=" * 50)

    # 检查 API Key 数量（仅提示）
    from app.ai.llm import get_api_key_count
    key_count = get_api_key_count()
    if key_count > 1:
        print(f"  已配置 {key_count} 个 API Key，自动轮询负载均衡")
    elif key_count == 1:
        print(f"  已配置 1 个 API Key")
    else:
        print(f"  未配置 API Key，AI 功能不可用")

    # 启动时异步学习全局聊天知识
    try:
        from app.ai.global_learner import train_global_knowledge
        train_global_knowledge()
    except Exception as e:
        print(f"  [WARN] 全局学习失败: {e}")

    # ---- 控制台指令监听 ----
    import threading as _threading
    import time as _time

    _shutdown_requested = False  # 非本地变量，闭包引用

    def _handle_cmd(cmd):
        """处理控制台命令"""
        nonlocal _shutdown_requested
        if cmd == 'stop':
            _shutdown_requested = True
            # 将 shutdown 标记为全局可访问，让 uvicorn 触发优雅关闭
            print("\n  ╔══════════════════════════╗")
            print("  ║   正在优雅关闭程序...     ║")
            print("  ╚══════════════════════════╝")
            # 关闭聊天大厅所有连接
            try:
                with _chat_lobby_lock:
                    lobby_users = list(_chat_lobby_users.values())
                for u in lobby_users:
                    try:
                        asyncio.get_event_loop().call_soon_threadsafe(
                            lambda ws=u["websocket"]: asyncio.ensure_future(ws.close())
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            # 触发 uvicorn 退出
            if uvicorn_server:
                uvicorn_server.should_exit = True
        elif cmd == 'chongqi':
            print("\n  ╔══════════════════════════╗")
            print("  ║   正在重启程序...         ║")
            print("  ╚══════════════════════════╝")
            # 确定启动脚本路径
            _run_script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run.py")
            if not os.path.isfile(_run_script):
                _run_script = ""
            if _run_script:
                _cwd = os.path.dirname(_run_script)
                if sys.platform == "win32":
                    try:
                        # 1. 先启动新进程（detached）
                        subprocess.Popen(
                            [sys.executable, _run_script],
                            cwd=_cwd,
                            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                        )
                    except Exception as e:
                        print(f"  启动新进程失败: {e}")
                        print("  请手动重启: python run.py")
                        return
                else:
                    try:
                        os.execv(sys.executable, [sys.executable, _run_script])
                    except Exception as e:
                        print(f"  重启失败: {e}")
                        print("  请手动重启: python run.py")
                        return
            else:
                print("  无法确定启动脚本，请手动重启（Ctrl+C 然后重新运行）")
                return
            # 2. 优雅退出当前进程（不硬杀，让 TCP 正常关闭）
            _shutdown_requested = True
            if uvicorn_server:
                uvicorn_server.should_exit = True
        elif cmd:
            print(f"  未知命令: '{cmd}'  (可用: stop, chongqi)")

    def _console_listener_win():
        """Windows: 用 msvcrt 直接读控制台，绕过 stdin 被子进程占用的问题"""
        import msvcrt
        print("  输入 stop 关闭程序 | 输入 chongqi 重启程序")
        buf = ""
        while True:
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ('\r', '\n'):
                        print()
                        _handle_cmd(buf.strip().lower())
                        buf = ""
                    elif ch == '\x03':  # Ctrl+C
                        raise KeyboardInterrupt
                    elif ch == '\x08':  # Backspace
                        if buf:
                            buf = buf[:-1]
                            print('\b \b', end='', flush=True)
                    elif ch.isprintable():
                        buf += ch
                        print(ch, end='', flush=True)
                else:
                    _time.sleep(0.05)
            except KeyboardInterrupt:
                break
            except Exception as e:
                # 兜底：任何异常都不让线程退出，打印后继续
                print(f"  [CONSOLE] 监听异常: {e}")
                _time.sleep(0.5)

    def _console_listener_unix():
        """Unix: 用标准 input()"""
        print("  输入 stop 关闭程序 | 输入 chongqi 重启程序")
        while True:
            try:
                cmd = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            except Exception as e:
                print(f"  [CONSOLE] 监听异常: {e}")
                _time.sleep(0.5)
                continue
            _handle_cmd(cmd)

    def _start_console_listener():
        """启动控制台监听线程，自动重启（防止线程意外死亡导致控制台无响应）"""
        while True:
            try:
                if sys.platform == "win32":
                    t = _threading.Thread(target=_console_listener_win, daemon=True)
                else:
                    t = _threading.Thread(target=_console_listener_unix, daemon=True)
                t.start()
                t.join()  # 阻塞直到线程结束
                print("  [CONSOLE] 监听线程已退出，2秒后自动重启...")
            except Exception as e:
                print(f"  [CONSOLE] 启动监听失败: {e}")
            _time.sleep(2)

    _listener_thread = _threading.Thread(target=_start_console_listener, daemon=True)
    _listener_thread.start()

    # 启动在线人数历史记录线程（每 60 秒）
    _online_recorder = _threading.Thread(target=_record_online_count, daemon=True)
    _online_recorder.start()

    # 启动 uvicorn
    # 注意：如需开发模式热重载，设置环境变量 TURING_RELOAD=1
    host = host or AI_CONFIG.get("host", "0.0.0.0")
    port = port or AI_CONFIG.get("port", 1234)
    use_reload = os.environ.get("TURING_RELOAD", "0") == "1"
    config = uvicorn.Config(
        "app.main:app",
        host=host,
        port=port,
        reload=use_reload,
        reload_dirs=["app"] if use_reload else None,
        access_log=False,
        reload_delay=0.5,
        ws_ping_interval=60,
        ws_ping_timeout=60,
    )
    global uvicorn_server
    uvicorn_server = uvicorn.Server(config)

    # ---- 注册信号处理器（优雅关闭）----
    import signal as _signal

    def _signal_handler(sig, frame):
        """SIGTERM/SIGINT: 触发优雅关闭"""
        print("\n  收到终止信号，正在优雅关闭...")
        if uvicorn_server:
            uvicorn_server.should_exit = True

    try:
        _signal.signal(_signal.SIGINT, _signal_handler)
        _signal.signal(_signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        # 非主线程或某些平台不支持
        pass

    try:
        uvicorn_server.run()
    except KeyboardInterrupt:
        print("\n  服务已停止")
    finally:
        print("  [SHUTDOWN] 清理完成，再见！")
