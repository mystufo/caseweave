"""Centralized logging setup. Imported once from main.py."""
import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    fmt = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
    datefmt = "%H:%M:%S"

    root = logging.getLogger()
    root.handlers.clear()
    # 走 stderr 而不是 stdout：uvicorn 的 access/error log 也都是 stderr，
    # 一处合流方便重定向；同时绕过 stdout 在管道下的块缓冲，让日志实时可见
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet down chatty libraries; keep our app + uvicorn at INFO.
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)
