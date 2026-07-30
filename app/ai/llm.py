"""
图灵测试 · LLM 调用层
封装 OpenAI 兼容接口（含连接池、多Key轮询、超时重试）
"""
import json
import asyncio
import httpx
from itertools import cycle
from app.config import AI_CONFIG
from app.logger import error_log

# ---- 全局连接池（复用 TCP 连接，避免每次新建）----
_shared_client: httpx.AsyncClient | None = None
_client_lock: asyncio.Lock | None = None


def _get_client_lock() -> asyncio.Lock:
    """惰性获取客户端锁（避免模块加载时无事件循环）"""
    global _client_lock
    if _client_lock is None:
        _client_lock = asyncio.Lock()
    return _client_lock


async def _get_client() -> httpx.AsyncClient:
    """获取共享 httpx 客户端（连接池），自动检测已关闭的连接"""
    global _shared_client
    if _shared_client is not None and not _shared_client.is_closed:
        return _shared_client
    async with _get_client_lock():
        if _shared_client is not None and not _shared_client.is_closed:
            return _shared_client
        limits = httpx.Limits(
            max_keepalive_connections=20,  # 提高复用，减少 TCP 握手
            max_connections=50,
            keepalive_expiry=30.0,
        )
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0, read=120.0, write=60.0, pool=15.0),
            limits=limits,
        )
    return _shared_client


async def _close_client():
    """关闭共享客户端（在应用关闭时调用）"""
    global _shared_client
    if _shared_client:
        await _shared_client.aclose()
        _shared_client = None


# ---- 多 API Key 轮询管理器 ----
_api_key_cycle: cycle | None = None
_api_key_list: list[str] = []
_api_key_index = 0


def _parse_api_keys(api_key_str: str) -> list[str]:
    """解析逗号分隔的 API Key，返回去重非空列表"""
    if not api_key_str:
        return []
    keys = [k.strip() for k in api_key_str.split(",") if k.strip()]
    seen = set()
    result = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def _refresh_key_cycle():
    """刷新密钥轮询列表（配置变化时调用）"""
    global _api_key_cycle, _api_key_list, _api_key_index
    keys = _parse_api_keys(AI_CONFIG.get("api_key", ""))
    if keys != _api_key_list:
        _api_key_list = keys
        _api_key_index = 0
    if keys:
        _api_key_cycle = cycle(keys)


def _get_next_api_key() -> str:
    """获取下一个 API Key（轮询）"""
    global _api_key_index, _api_key_list
    if not _api_key_list:
        _refresh_key_cycle()
    if not _api_key_list:
        return ""
    # 原子轮询
    key = _api_key_list[_api_key_index]
    _api_key_index = (_api_key_index + 1) % len(_api_key_list)
    return key


def get_all_api_keys() -> list[str]:
    """获取当前所有有效 API Key"""
    _refresh_key_cycle()
    return list(_api_key_list)


def get_api_key_count() -> int:
    """获取当前 API Key 数量"""
    return len(_api_key_list)


# ---- 构建请求通用参数 ----
def _build_request_json(
    messages: list,
    max_tokens_override: int | None = None,
    stream: bool = False,
    tools: list | None = None,
) -> dict:
    """构建请求体"""
    body = {
        "model": AI_CONFIG.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": float(AI_CONFIG.get("temperature", 0.7)),
        "max_tokens": max_tokens_override or int(AI_CONFIG.get("max_tokens", 200)),
    }
    if stream:
        body["stream"] = True
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return body


async def _do_request(
    messages: list,
    max_tokens_override: int | None = None,
    stream: bool = False,
    tools: list | None = None,
    retries: int = 3,
) -> tuple[httpx.Response, str]:
    """
    执行一次 API 请求，自动轮询 API Key
    返回 (response, used_api_key)
    """
    base_url = str(AI_CONFIG.get("base_url", "")).rstrip("/")
    _refresh_key_cycle()

    if not _api_key_list:
        raise ValueError("未配置 api_key（可在 config.json 中用逗号分隔多个 Key）")

    client = await _get_client()
    last_err = None

    # 尝试所有可用 API Key（每个 Key 最多重试 retries 次）
    tried_keys = set()
    for _ in range(len(_api_key_list)):
        api_key = _get_next_api_key()
        if api_key in tried_keys:
            # 已经轮询了一圈，所有 Key 都试过了
            break
        tried_keys.add(api_key)
        for attempt in range(retries):
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                req_data = _build_request_json(messages, max_tokens_override, stream, tools)
                if stream:
                    resp = client.stream("POST", f"{base_url}/chat/completions", headers=headers, json=req_data)
                    return resp, api_key
                else:
                    resp = await client.post(f"{base_url}/chat/completions", headers=headers, json=req_data)
                    resp.raise_for_status()
                    return resp, api_key
            except httpx.HTTPStatusError as e:
                last_err = e
                status = e.response.status_code
                if status in (401, 403):
                    # Key 无效，换下一个
                    error_log("LLM", f"API Key 无效(HTTP {status})，自动切换")
                    break  # 跳出 retry 循环，换下一个 Key
                if status in (400,):
                    try:
                        detail = e.response.text[:200]
                    except Exception:
                        detail = ""
                    raise RuntimeError(f"大模型返回 HTTP 400：{detail}") from e
                if status == 429 or status >= 500:
                    # 限流或服务端错误，重试
                    wait = 1.5 * (attempt + 1)
                    error_log("LLM", f"HTTP {status}，{wait:.1f}s 后重试({attempt+1}/{retries})")
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"大模型返回 HTTP {status}") from e
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                wait = 1.5 * (attempt + 1)
                error_log("LLM", f"网络错误，{wait:.1f}s 后重试({attempt+1}/{retries})")
                await asyncio.sleep(wait)
            except (httpx.RemoteProtocolError, httpx.LocalProtocolError) as e:
                last_err = e
                error_log("LLM", f"连接异常，切换 Key 重试: {e}")
                break  # 连接异常，换 Key
        # 所有重试用完，尝试下一个 Key

    raise RuntimeError(f"所有 API Key 均请求失败: {last_err}") from last_err


async def chat_completion(messages: list, max_tokens_override: int = None) -> str:
    """非流式 chat completion"""
    resp, _ = await _do_request(messages, max_tokens_override, stream=False)
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        error_log("LLM", f"响应不是合法 JSON: {resp.text[:200]}")
        raise RuntimeError("AI 返回了非 JSON 响应") from None
    return data["choices"][0]["message"]["content"].strip().replace("\n", "")


async def test_connection() -> str:
    """用当前配置验证连通性"""
    return await chat_completion(
        messages=[{"role": "user", "content": "你好，用一句话回复我。"}],
        max_tokens_override=50,
    )


async def chat_completion_stream(messages: list, on_token, max_tokens_override: int = None) -> str:
    """流式 chat completion"""
    resp, _ = await _do_request(messages, max_tokens_override, stream=True, retries=2)
    full = []
    try:
        async with resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_text = line[6:]
                if data_text == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_text)
                    token = chunk["choices"][0]["delta"].get("content", "")
                except Exception:
                    continue
                if token:
                    full.append(token)
                    on_token(token)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            raise RuntimeError("API Key 无效，自动切换后仍失败") from e
        raise
    return "".join(full).strip().replace("\n", "")


async def chat_with_tools(
    messages: list,
    tools: list,
    tool_handler,
    max_turns: int = 5,
    max_tokens_override: int = None,
) -> str:
    """带 function calling 的 agent loop"""
    for turn in range(max_turns):
        try:
            resp, used_key = await _do_request(messages, max_tokens_override, tools=tools)
        except RuntimeError:
            # 降级：模型不支持 tools
            try:
                # 再试一次不带 tools
                return await chat_completion(messages, max_tokens_override=max_tokens_override)
            except Exception:
                return ""

        try:
            response = resp.json()["choices"][0]["message"]
        except Exception as e:
            error_log("LLM", f"解析工具响应失败: {e}")
            return ""

        content = response.get("content", "") or ""
        tool_calls = response.get("tool_calls") or []

        if not tool_calls:
            return content.strip()

        tools_msg = {"role": "assistant", "content": content, "tool_calls": tool_calls}
        messages.append(tools_msg)

        for tc in tool_calls:
            func_name = tc["function"]["name"]
            try:
                func_args = json.loads(tc["function"].get("arguments", "{}"))
            except Exception:
                func_args = {}
            try:
                result = await tool_handler(func_name, func_args)
            except Exception as e:
                result = f"工具执行错误: {e}"
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})

    messages.append({"role": "user", "content": "请基于以上工具结果，直接输出你的最终分析结论。"})
    return await chat_completion(messages, max_tokens_override=max_tokens_override)
