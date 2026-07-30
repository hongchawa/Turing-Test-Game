"""
图灵测试 · MaiBot 风格日志系统
带时间戳、模块名、着色、结构化输出
"""
import sys
import colorama
from colorama import Fore, Style

# 初始化 colorama：将 ANSI 转义码转为 Windows 控制台 API 调用
# strip=False: 保留 ANSI 码（convert=True 时会自动转换为 Windows API）
# convert=True: 在 Windows 上将 ANSI 转义为控制台 API
colorama.init(strip=False, convert=True)

import logging
import time
from datetime import datetime

# 终端颜色（使用 colorama 常量，跨平台兼容）
RESET = Fore.RESET
COLORS = {
    "DEBUG": Fore.LIGHTBLACK_EX,
    "INFO": "",
    "CHAT": Fore.CYAN,
    "THINK": Fore.LIGHTBLACK_EX,
    "PLAN": Fore.CYAN,
    "EXPRESS": Fore.YELLOW,
    "LEARN": Fore.MAGENTA,
    "REPLY": Fore.GREEN,
    "WARNING": Fore.LIGHTYELLOW_EX,
    "ERROR": Fore.RED,
    "SUCCESS": Fore.LIGHTGREEN_EX,
    "SYSTEM": Fore.BLUE,
    "BOLD": Fore.RESET + Style.BRIGHT,
    "DIM": Fore.RESET + Style.DIM,
}

SEP = "─" * 50
SEP_THIN = "─" * 30

# 确保 stdout 是 UTF-8 编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class MaiBotFormatter(logging.Formatter):
    """模仿 MaiBot 的日志格式：时间 | 模块 | 级别 | 消息"""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        module = getattr(record, "module_name", record.name)
        level = record.levelname
        color = COLORS.get(level, "")
        msg = record.getMessage()
        return f"{color}{ts} | {module:<20} | {msg}{RESET}"


def setup_logger(name: str = "maisaka") -> logging.Logger:
    """创建 MaiBot 风格的 logger"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(MaiBotFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger


def _print_color(*args, **kwargs):
    """带强制刷新的彩色打印"""
    print(*args, **kwargs, flush=True)


def chat_log(player_name: str, tag: str, message: str, level: str = "CHAT"):
    """聊天流日志：[玩家名] 标签 内容"""
    color = COLORS.get(level, "")
    name_color = COLORS["BOLD"]
    ts = datetime.now().strftime("%H:%M:%S")
    _print_color(f"{COLORS['DIM']}{SEP_THIN}{RESET}")
    _print_color(f"{color}{ts} | {name_color}{player_name}{color} | {tag} {message}{RESET}")


def think_log(message: str):
    """思考日志（raw 回复）"""
    _print_color(f"  {COLORS['THINK']}[THINK] raw: {message}{RESET}")


def plan_log(message: str):
    """规划器日志（Planner 分析）"""
    _print_color(f"  {COLORS['PLAN']}[PLAN] 分析: {message}{RESET}")


def express_log(message: str):
    """润色日志（expressor 输出）"""
    _print_color(f"  {COLORS['EXPRESS']}[EXPR] expr: {message}{RESET}")


def learn_log(expressions: list, jargon: list):
    """学习日志"""
    color = COLORS["LEARN"]
    if expressions:
        _print_color(f"  {color}[LEARN] 学了: {expressions[0]}{RESET}")
    if jargon:
        _print_color(f"  {color}[LEARN] 口头禅: {', '.join(jargon)}{RESET}")


def reply_log(player_name: str, reply: str):
    """最终回复日志"""
    color = COLORS["REPLY"]
    _print_color(f"  {color}[REPLY] ({len(reply)}字) {reply}{RESET}")
    _print_color(f"{COLORS['DIM']}{SEP_THIN}{RESET}")


def error_log(tag: str, message: str):
    """错误日志"""
    _print_color(f"  {COLORS['ERROR']}[ERROR] {tag}: {message}{RESET}")


def system_log(message: str):
    """系统日志"""
    color = COLORS["SYSTEM"]
    ts = datetime.now().strftime("%H:%M:%S")
    _print_color(f"{color}{ts} | {'system':20} | {message}{RESET}")
