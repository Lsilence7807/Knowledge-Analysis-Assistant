# 文件：backend/app/core/exec.py
# 作用：阻塞任务的统一执行器——DuckDB 查询、pandas、模型 HTTP、子进程都经 run_blocking() 排队进同一个固定线程池
# 阶段：P21 性能与并发契约
# 依赖：标准库 concurrent.futures、atexit、backend/app/core/config.py
from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from app.core import config

_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    """线程池惰性建一次；大小取 config.EXEC_MAX_WORKERS（改环境变量需重启进程才生效）。"""
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=config.EXEC_MAX_WORKERS, thread_name_prefix="exec")
        atexit.register(shutdown)
    return _pool


def run_blocking(fn, /, *args, **kwargs):
    """把阻塞函数丢进固定线程池执行并等结果，池满就排队；异常与返回值与直接调用一致。"""
    # ponytail: 线程池单进程，p95 不达标再上进程池或只读副本（§4.14 给的升级路径）
    return _executor().submit(partial(fn, *args, **kwargs)).result()


def shutdown() -> None:
    """关掉线程池并置空，下次调用重建（测试收尾与进程退出用）。"""
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=True)
        _pool = None
