# -*- coding: utf-8 -*-
"""本地服务启动器：记录启动结果、确认就绪、打开网页并保留停止说明。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
# 本机敏感运行数据：状态含 PID/日志绝对路径，启动日志含解释器路径、URL 和异常堆栈。
# 不提交 data/，对外分享控制台输出前也须脱敏；详见 00_项目管理/安全与公开仓库管理.md。
STATUS_FILE = DATA / "service-status.json"
LOG_FILE = DATA / "startup.log"
REQUIRED = ("flask", "selenium", "lxml", "requests")


def write_status(state: str, **details) -> None:
    payload = {"state": state, "pid": os.getpid(), "updated_at": int(time.time()),
               "log": str(LOG_FILE), **details}
    pending = STATUS_FILE.with_suffix(".tmp")
    pending.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(STATUS_FILE)


def healthy(url: str) -> dict | None:
    with suppress(OSError, ValueError, urllib.error.URLError):
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            result = json.load(response)
            if result.get("ok") and result.get("project_root") == str(ROOT.parent):
                return result
    return None


def existing_service() -> str | None:
    if STATUS_FILE.exists():
        with suppress(ValueError, OSError):
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            url = status.get("url", "")
            if url.startswith("http://127.0.0.1:"):
                health = healthy(url)
                if health:
                    if health.get("data_scope") != "crawler-only-v1":
                        raise SystemExit(f"旧版服务仍在运行：{url}。请在旧服务窗口按 Ctrl+C 停止，再重新启动，以应用数据访问边界修复。")
                    return url
    return None


def open_page(url: str, disabled: bool = False) -> None:
    if disabled:
        return
    logging.info("正在打开网页：%s", url)
    if not webbrowser.open(url):
        logging.warning("浏览器未能自动打开，请复制上方地址手动访问。")


def serve(port: int | None, no_browser: bool) -> int:
    from werkzeug.serving import make_server
    from server.app import create_app

    current = existing_service()
    if current:
        logging.info("本项目已经运行：%s（不会重复启动）", current)
        open_page(current, no_browser)
        return 0
    write_status("starting")
    logging.info("启动中：正在加载采集器、数据库与账号池……")
    app = create_app()
    server = None
    ports = [port] if port else range(8765, 8805)
    for candidate in ports:
        # 绑定成功才返回，避免探测后再绑定的竞争。
        try:
            server = make_server("127.0.0.1", candidate, app, threaded=True)
        except (OSError, SystemExit):
            logging.info("端口 %s 无法使用，尝试下一个端口。", candidate)
            continue
        break
    if server is None:
        raise RuntimeError("没有可用端口；请查看日志，关闭端口占用程序后重试。")
    url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="zforge-http")
    thread.start()
    if not healthy(url):
        server.shutdown()
        server.server_close()
        raise RuntimeError("服务未通过就绪检查，请查看启动日志。")
    write_status("running", url=url)
    logging.info("已就绪：%s", url)
    logging.info("账号池：%s/#/accounts", url)
    logging.info("关闭网页不会停止服务。在本窗口按 Ctrl+C 停止；请保持本窗口打开。")
    logging.info("状态文件：%s", STATUS_FILE)
    open_page(url, no_browser)
    stopped = threading.Event()
    try:
        while thread.is_alive():
            stopped.wait(1)
    except KeyboardInterrupt:
        logging.info("正在停止服务，取消未完成任务……")
    finally:
        ext = app.extensions["zforge"]
        ext["logins"].shutdown()
        for run in ext["workflows"].list_runs():
            if run["state"] == "running":
                ext["workflows"].cancel(run["run_id"])
        ext["runner"].shutdown(wait=True)
        server.shutdown()
        server.server_close()
        ext["store"].close()
        ext["pool"].close()
        write_status("stopped", url=url)
        logging.info("已停止：%s。重新双击启动.bat 即可再次运行。", url)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动 ZForge 本地工作台")
    parser.add_argument("--check", action="store_true", help="仅检查环境，不启动服务")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--port", type=int, help="使用指定端口")
    args = parser.parse_args()
    DATA.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S",
                        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")])
    logging.info("ZForge · Python %s · %s", sys.version.split()[0], sys.executable)
    if args.port is not None and not 1 <= args.port <= 65535:
        logging.error("端口必须在 1～65535 之间。")
        write_status("failed", error="端口必须在 1～65535 之间")
        raise SystemExit(2)
    missing = [name for name in REQUIRED if importlib.util.find_spec(name) is None]
    if missing:
        logging.error('缺少依赖：%s。请执行："%s" -m pip install -r "%s"',
                      ", ".join(missing), sys.executable, ROOT / "requirements.txt")
        write_status("failed", error="缺少依赖：" + ", ".join(missing))
        raise SystemExit(1)
    logging.info("环境检查通过。日志：%s", LOG_FILE)
    if args.check:
        raise SystemExit(0)
    try:
        raise SystemExit(serve(args.port, args.no_browser))
    except Exception:
        logging.error("启动或运行失败，详细原因如下：\n%s", traceback.format_exc())
        write_status("failed", error="启动或运行失败，详见日志")
        logging.error("窗口将保留。请查看日志：%s", LOG_FILE)
        raise SystemExit(1)
