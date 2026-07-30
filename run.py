#!/usr/bin/env python3
"""
图灵测试 · 一键启动
自动创建虚拟环境 + 换清华源安装依赖 + 解除端口占用 + 启动服务
用法: python run.py
"""
import subprocess
import sys
import os
import signal
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 控制台编码设置（解决 Windows 中文乱码）----
if sys.platform == "win32":
    # 设置控制台代码页为 UTF-8
    subprocess.run("chcp 65001", shell=True, capture_output=True)
    # 设置 Python IO 编码
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    # 启用 Windows 虚拟终端处理（支持 ANSI 颜色）
    import ctypes
    kernel32 = ctypes.windll.kernel32
    STD_OUTPUT_HANDLE = -11
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    mode = ctypes.c_uint32()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING)

VENV = os.path.join(HERE, "venv")
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 从 config.json 读取端口
def _get_port() -> int:
    try:
        import json as _json
        cfg_path = os.path.join(HERE, "config.json")
        if os.path.exists(cfg_path):
            with open(cfg_path, "r") as f:
                return _json.load(f).get("port", 1234)
    except Exception:
        pass
    return 1234

PORT = _get_port()


def _venv_python() -> str:
    if sys.platform == "win32":
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def _venv_pip() -> str:
    if sys.platform == "win32":
        return os.path.join(VENV, "Scripts", "pip.exe")
    return os.path.join(VENV, "bin", "pip")


def _ensure_venv():
    """创建虚拟环境（含 pip）"""
    if not os.path.exists(_venv_python()):
        print("创建虚拟环境...")
        subprocess.check_call([sys.executable, "-m", "venv", VENV, "--clear"])

    python = _venv_python()
    if not os.path.exists(_venv_pip()):
        print("安装 pip...")
        subprocess.check_call([python, "-m", "ensurepip", "--upgrade", "--default-pip"])

    # 升级 pip 到最新
    subprocess.check_call([_venv_pip(), "install", "--upgrade", "pip", "-q", "-i", MIRROR])


def _install_deps():
    """用清华源安装依赖"""
    req = os.path.join(HERE, "requirements.txt")
    print("安装依赖（清华源）...")
    subprocess.check_call([_venv_pip(), "install", "-r", req, "-q", "-i", MIRROR])
    print("依赖就绪")


def _free_port():
    """自动解除端口占用（跨平台）"""
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                f"netstat -ano | findstr :{PORT}",
                capture_output=True, text=True, shell=True,
            )
            for line in result.stdout.strip().split("\n"):
                if f":{PORT} " not in f" {line} " and f":{PORT}\t" not in line:
                    continue
                parts = [p for p in line.strip().split() if p.isdigit()]
                if parts:
                    pid = parts[-1]
                    print(f"端口 {PORT} 被 PID {pid} 占用，自动释放...")
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    time.sleep(0.5)
        else:
            result = subprocess.run(
                ["lsof", f"-iTCP:{PORT}", "-sTCP:LISTEN", "-t", "-n", "-P"],
                capture_output=True, text=True,
            )
            for pid in result.stdout.strip().split("\n"):
                pid = pid.strip()
                if pid and pid.isdigit():
                    print(f"端口 {PORT} 被 PID {pid} 占用，自动释放...")
                    os.kill(int(pid), signal.SIGTERM)
                    time.sleep(0.5)
    except Exception:
        pass


def main():
    _ensure_venv()
    _install_deps()
    _free_port()

    print("启动图灵测试...")
    os.chdir(HERE)
    cmd = [_venv_python(), "-c", "from app.main import run; run()"]
    if sys.platform == "win32":
        # Windows: os.execv 不支持，改用 subprocess.call（继承 UTF-8 环境）
        try:
            sys.exit(subprocess.call(cmd))
        except KeyboardInterrupt:
            print("\n服务已停止")
            sys.exit(0)
    else:
        os.execv(_venv_python(), cmd)


if __name__ == "__main__":
    main()
