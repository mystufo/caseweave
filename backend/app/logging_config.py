"""Centralized logging setup. Imported once from main.py."""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional


def setup_logging(level: str = "INFO", log_file: Optional[str] = None) -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
    datefmt = "%H:%M:%S"
    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)

    root = logging.getLogger()
    root.handlers.clear()
    # 走 stderr 而不是 stdout：uvicorn 的 access/error log 也都是 stderr，
    # 一处合流方便重定向；同时绕过 stdout 在管道下的块缓冲，让日志实时可见
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level)

    # log_file 非空则日志同时落盘（自动轮转）。支持 ~ 展开；相对路径以进程 CWD 为基准。
    if log_file:
        path = os.path.expanduser(log_file.strip())
        if path:
            try:
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                # 单文件 10MB，保留 5 个历史，避免磁盘无限增长
                file_handler = RotatingFileHandler(
                    path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
                )
                file_handler.setFormatter(formatter)
                root.addHandler(file_handler)
                logging.getLogger("testcraft.main").info("日志同时写入文件: %s", os.path.abspath(path))
            except OSError as e:
                # 落盘失败绝不能拖垮启动 —— 退回到只走 stderr
                logging.getLogger("testcraft.main").warning(
                    "无法写入日志文件 %s（%s），仅输出到 stderr", path, e
                )

    # Quiet down chatty libraries; keep our app + uvicorn at INFO.
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
