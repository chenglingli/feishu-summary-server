"""Manage this checkout's local processes without touching externally started services."""

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
WEB = Path(os.environ.get("SUMMARY_WEB_DIR", str(SERVER.parent / "feishu-summary-web")))
STATE = SERVER / ".local-services"
NAMES = ("api", "web", "ngrok", "runner")


def stamp(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def alive(record):
    # PID alone is unsafe after a restart: verify process start time and command too.
    return bool(record and record.get("stamp") and stamp(record["pid"]) == record["stamp"])


def records():
    path = STATE / "processes.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save(data):
    temp = STATE / "processes.tmp"
    temp.write_text(json.dumps(data))
    temp.replace(STATE / "processes.json")


def commands():
    env = dict(os.environ)
    # Prefer Homebrew Node 22 even when an incompatible global Node comes first in PATH.
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        node_bin = prefix / "opt/node@22/bin"
        if (node_bin / "node").exists():
            env["PATH"] = str(node_bin) + os.pathsep + env.get("PATH", "")
            break
    node = shutil.which("node", path=env.get("PATH"))
    ngrok = shutil.which("ngrok", path=env.get("PATH"))
    if not node or not ngrok:
        raise RuntimeError("找不到 Node 22 或 ngrok，请先安装并配置 PATH。")
    version = subprocess.check_output([node, "--version"], env=env, text=True).strip()
    if not version.startswith("v22."):
        raise RuntimeError(f"需要 Node 22，当前为 {version}。")
    if not (SERVER / ".env").exists() or not (WEB / "node_modules/vite/bin/vite.js").exists():
        raise RuntimeError("请先配置后端 .env，并在前端执行 npm ci。")
    python = str(SERVER / ".venv/bin/python")
    return env, {
        "api": (
            [
                python,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--no-access-log",
            ],
            SERVER,
        ),
        "web": (
            [
                node,
                str(WEB / "node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                "5173",
                "--strictPort",
            ],
            WEB,
        ),
        "runner": ([python, "-m", "app.runner"], SERVER),
        "ngrok": ([ngrok, "http", "5173", "--log=stdout"], SERVER),
    }


def stop_one(name, data):
    record = data.get(name)
    if not alive(record):
        data.pop(name, None)
        save(data)
        return True
    os.kill(record["pid"], signal.SIGTERM)
    for _ in range(30):
        if not alive(record):
            data.pop(name, None)
            save(data)
            print(f"{name} 已停止")
            return True
        time.sleep(1)
    # A runner may be draining a real task. Never force-kill it or start a duplicate.
    print(f"{name} 正在退出，稍后用 status 查看；没有强制终止。")
    return False


def start(data):
    env, specs = commands()
    import socket

    for name, port in (("api", 8000), ("web", 5173)):
        if alive(data.get(name)):
            continue
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raise RuntimeError(
                    f"端口 {port} 已被占用，请先停止手动启动的 {name}；脚本不会接管或杀死它。"
                ) from None
    processes = subprocess.check_output(["ps", "-ax", "-o", "pid=,command="], text=True)
    for name, marker in (("runner", "-m app.runner"), ("ngrok", "ngrok http 5173")):
        if not alive(data.get(name)) and marker in processes:
            raise RuntimeError(f"发现脚本以外的 {name}，请先停止原进程。")
    # Readiness must be checked before a worker can run scheduled jobs.
    sys.path.insert(0, str(SERVER))
    os.chdir(SERVER)
    from sqlalchemy import text

    from app.db import engine

    with engine.connect() as connection:
        connection.execute(text("SELECT 1 FROM summary_runs LIMIT 1"))
    started = []
    try:
        for name in NAMES:
            if alive(data.get(name)):
                print(f"{name} 已运行，跳过")
                continue
            args, cwd = specs[name]
            with (STATE / f"{name}.log").open("ab") as log:
                process = subprocess.Popen(
                    args,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            time.sleep(1)
            if process.poll() is not None:
                raise RuntimeError(f"{name} 启动失败，请运行 logs {name} 查看日志。")
            data[name] = {"pid": process.pid, "stamp": stamp(process.pid)}
            save(data)
            started.append(name)
            print(f"{name} 已启动，PID {process.pid}")
        print("服务已放入后台。请用 status 检查健康状态；日志位于 " + str(STATE))
    except Exception:
        for name in reversed(started):
            stop_one(name, data)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="本地服务管理：start / stop / restart / status / logs"
    )
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "logs"))
    parser.add_argument("service", nargs="?", choices=NAMES)
    args = parser.parse_args()
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有服务管理命令正在执行，请稍后重试。") from None
        data = records()
        if args.action == "logs":
            for name in [args.service] if args.service else NAMES:
                print(f"--- {name} ---", flush=True)
                path = STATE / f"{name}.log"
                if path.exists():
                    subprocess.run(["tail", "-n", "40", str(path)], check=True)
            return
        if args.action == "status":
            for name in NAMES:
                record = data.get(name)
                print(
                    f"{name}: "
                    + (f"运行中 PID {record['pid']}" if alive(record) else "未由本脚本运行")
                )
            subprocess.run(
                ["curl", "--max-time", "3", "-fsS", "http://127.0.0.1:8000/health/ready"]
            )
            print()
            return
        if args.action in ("stop", "restart"):
            # Drain runner first; keep API and tunnel alive while it finishes.
            if not stop_one("runner", data):
                return 1
            for name in ("ngrok", "web", "api"):
                if not stop_one(name, data):
                    return 1
        if args.action in ("start", "restart"):
            start(data)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Database exceptions can embed connection details; do not print arbitrary exception text.
        print(
            str(error)
            if isinstance(error, RuntimeError)
            else f"操作失败（{type(error).__name__}），请检查环境、数据库和日志。",
            file=sys.stderr,
        )
        sys.exit(1)
