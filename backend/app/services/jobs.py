# 文件：backend/app/services/jobs.py
# 作用：后台作业：huey 队列（data/jobs.db）跑长分析、APScheduler 跑定时维护；进度与结果落 jobs 表
# 阶段：F9 作业与文档（兼 P18）
# 依赖：huey、apscheduler、app/core/{config,db}.py、app/services/{store,nlu,insight,agent}.py
from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from uuid import uuid4

from huey import SqliteHuey

from app.core import config, db
from app.services import agent, insight, nlu, store

logger = logging.getLogger(__name__)

KINDS = ("ask",)
BACKGROUND_AFTER_S = 10.0  # P18：预判超过 10s 的分析转后台，快作业当着请求跑完
BASE_ASK_S = 2.0  # 单跳的固定开销（一次模型往返）
AGENT_ASK_S = 6.0  # agent 的固定开销（多轮模型往返）
ROWS_PER_SECOND = 20000.0  # 本机经验值：两万行聚合约 1s，用来把行数折成秒
RESULT_ROWS = 200  # 作业结果表带回的行数上限（与 config/tools.json 的单步上限同口径）
PRUNE_DAYS = 7  # 定时维护保留多少天

_queues: dict[str, tuple[SqliteHuey, object]] = {}
_worker: threading.Thread | None = None
_consumer = None
_scheduler = None


def enabled() -> bool:
    """能力开关（§6）：ENABLE_JOBS 打开才有后台作业，关闭时 /jobs 回 503。"""
    return bool(config.ENABLE_JOBS)


def _queue() -> tuple[SqliteHuey, object]:
    """按当前配置取 huey 实例与任务句柄。

    路径变了就重建：测试会把 DATA_DIR 换到临时目录，作业库得跟着走，不能落到仓库的 data/ 里。
    任务体注册在实例上（每个库一个实例），所以不会出现同名任务重复注册。
    """
    path = str(config.HUEY_DB)
    if path not in _queues:
        queue = SqliteHuey(name="kaa", filename=path, immediate=False, utc=True)
        _queues[path] = (queue, queue.task(name="run_job")(_execute))
    return _queues[path]


def estimate_seconds(payload: dict) -> float:
    """预判一次分析要多久（P18「预判耗时 > 10s 转后台」）：只看数据集行数与是否走 agent。

    确定性启发式，宁可粗——它只决定「当场跑」还是「进队列」，判错最多是慢一点或白占一个队列位。
    """
    rows = float(payload.get("rows") or 0)
    base = AGENT_ASK_S if config.ENABLE_AGENT else BASE_ASK_S
    return base + rows / ROWS_PER_SECOND


def worker_alive() -> bool:
    """队列消费者在跑吗；不在就同步跑（F9 退化口径：huey 不可用时同步阻塞执行）。"""
    return bool(_worker and _worker.is_alive())


def _should_queue(payload: dict) -> bool:
    """两个条件都满足才进队列：预判超阈值、且消费者确实在跑；否则当场跑完。"""
    return estimate_seconds(payload) > BACKGROUND_AFTER_S and worker_alive()


def submit(kind: str, payload: dict) -> str:
    """提交一个后台作业并返回 job_id（§4.2）。同一入参已有排队/执行中的作业时回同一个 id。"""
    if kind not in KINDS:
        raise ValueError(f"不支持的作业类型：{kind or '空'}（支持 {'/'.join(KINDS)}）")
    payload = dict(payload or {})
    existing = store.find_active_job(kind, payload)
    if existing is not None:
        return existing["id"]
    job_id = f"j_{uuid4().hex[:8]}"
    queued = _should_queue(payload)
    store.insert_job({"id": job_id, "kind": kind, "payload": payload, "status": "queued" if queued else "running"})
    if queued:
        try:
            _queue()[1](job_id)  # TaskWrapper 调用＝入队；真正执行交给消费者线程
        except Exception as exc:  # noqa: BLE001  队列写不进去（磁盘满/锁住）：当着请求跑完，别让作业卡在 queued
            logger.warning("作业入队失败，改为同步执行：%s", exc)
            _execute(job_id)
    else:
        _execute(job_id)
    return job_id


def run_now(job_id: str) -> None:
    """当场跑一个作业（同步退化路径与测试用）：不排队、不依赖消费者。"""
    _execute(job_id)


def progress(job_id: str) -> dict:
    """作业的进度与结果（§4.2）；查不到抛 KeyError，路由翻成 404。"""
    job = store.get_job(job_id)
    if job is None:
        raise KeyError(job_id)
    return {
        "job_id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "progress": job["progress"],
        "result": job["result"],
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }


def _execute(job_id: str) -> None:
    """任务体：按 job_id 取 payload 再执行（队列里不传大对象），全程状态落库。"""
    job = store.get_job(job_id)
    if job is None or job["status"] in ("done", "failed", "interrupted"):
        return
    store.update_job(job_id, status="running", progress=0.1, error="")
    try:
        result = _dispatch(job["kind"], job["payload"])
    except Exception as exc:  # noqa: BLE001  作业失败不是崩溃：状态落 failed，原因留给 GET /jobs/{id}
        store.update_job(job_id, status="failed", progress=1.0, error=str(exc))
        return
    store.update_job(job_id, status="done", progress=1.0, result=result)


def _dispatch(kind: str, payload: dict) -> dict:
    """作业类型 → 实现；现在只有 ask（一次完整分析）。"""
    if kind == "ask":
        return _run_ask(payload)
    raise ValueError(f"不支持的作业类型：{kind}")


def _run_ask(payload: dict) -> dict:
    """跑一次分析（P18 的长任务形态）：模型 → SQL → 结果表 → 结论。

    口径与 /ask 的两条通道一致：结论失败只降级，结果表照给。
    """
    question = str(payload.get("question") or "").strip()
    dataset_id = str(payload.get("dataset_id") or "")
    if not question:
        raise ValueError("问题不能为空")
    dataset = store.get_dataset(dataset_id)
    if dataset is None:
        raise ValueError(f"数据集不存在：{dataset_id or '空'}")
    profile = {**dataset["profile"], "table": dataset["table_name"]}
    if config.ENABLE_AGENT:
        table = asdict(agent.run(question, payload.get("session_id"), dataset_id))
    else:
        table = _single_shot(question, profile)
    body = {
        "question": question,
        "dataset_id": dataset_id,
        "sql": table.get("sql") or "",
        "columns": table.get("columns") or [],
        "rows": table.get("rows") or [],
        "row_count": table.get("row_count") or 0,
        "truncated": bool(table.get("truncated")),
        "degraded": list(table.get("degraded") or []),
    }
    try:
        found = insight.summarize(question, body, profile, [])
        body["insight"] = found.model_dump() if hasattr(found, "model_dump") else found
    except Exception as exc:  # noqa: BLE001  结论失败照旧给结果表，降级原因写进 degraded（与 /ask 口径一致）
        body["degraded"] = [*body["degraded"], f"insight:{exc}"]
    return body


def _single_shot(question: str, profile: dict) -> dict:
    """单跳通道（ENABLE_AGENT=false 或 agent 不可用）：模型直出 SQL 再跑一次。"""
    return db.exec_sql(nlu.to_sql(question, profile, []), limit=RESULT_ROWS)


def start_worker() -> threading.Thread:
    """起队列消费者线程（消费 data/jobs.db）；已经活着就不重复起。"""
    global _worker, _consumer
    if _worker is not None and _worker.is_alive():
        return _worker
    from huey.consumer import Consumer

    _consumer = Consumer(_queue()[0], workers=1, periodic=False, initial_delay=0.2)
    # 嵌入式消费者：进程是 uvicorn 的，信号得留给应用自己处理，线程里也注册不了（signal.signal 只认主线程）
    _consumer._set_signal_handlers = lambda: None
    _worker = threading.Thread(target=_consumer.run, name="kaa-huey", daemon=True)
    _worker.start()
    return _worker


def stop_worker() -> None:
    """停消费者（进程退出或测试收尾时调）：只置停标志，不等正在跑的作业。"""
    global _consumer
    if _consumer is not None:
        try:
            _consumer.stop()
        finally:
            _consumer = None


def start_scheduler():
    """起 APScheduler（定时维护）：每天清一次过期作业；P23 的数据源定时同步以后挂这里。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(prune, "interval", hours=24, args=[PRUNE_DAYS], id="prune_jobs", replace_existing=True)
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    """停定时维护（进程退出或测试收尾时调）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def prune(days: int = PRUNE_DAYS) -> int:
    """清掉 N 天前的终态作业。"""
    return store.prune_jobs(int(days))
