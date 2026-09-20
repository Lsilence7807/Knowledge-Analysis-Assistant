# 文件：bench/bench.py
# 作用：本地性能基线——造 50 万行表量聚合查询 p50/p95，写 bench/baseline.json，比上次退化就非零退出
# 阶段：P21 性能与并发契约
# 依赖：duckdb、标准库 json/statistics/tempfile、app/config.py、app/db.py、app/exec.py
from __future__ import annotations

import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# 直接 python bench/bench.py 时仓库根不在 sys.path（同 K-027 的坑），先补上再 import app
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from app import config, db  # noqa: E402
from app import exec as exec_pool  # noqa: E402

ROWS = 500_000
RUNS = 20
BUDGET_S = 1.5  # §4.14：单表 50 万行聚合 p95 ≤ 1.5s
REGRESSION_RATIO = 1.3  # ponytail: p95 在多核机上会抖，超上次 30% 才算退化；换机器后按实测调
SQL = "SELECT bucket, COUNT(*) AS n, AVG(amount) AS amount FROM perf GROUP BY bucket ORDER BY bucket"
BASELINE = Path(__file__).resolve().parent / "baseline.json"


def prepare(tmp: Path) -> None:
    """造压测库：config 指向临时目录，建 1000 个分桶的 50 万行表（不碰仓库里的 data/）。"""
    config.DATA_DIR = tmp
    config.DUCKDB_PATH = tmp / "analytics.duckdb"
    config.SQLITE_PATH = tmp / "meta.sqlite"
    with duckdb.connect(str(config.DUCKDB_PATH)) as conn:
        conn.execute(
            f"CREATE TABLE perf AS SELECT (range % 1000) AS bucket, (range % 97) * 1.5 AS amount FROM range({ROWS})"
        )


def p95(samples: list[float]) -> float:
    """取第 95 百分位（升序样本）；样本太少时退化成最大值，避免空列表。"""
    ordered = sorted(samples)
    return ordered[max(0, round(len(ordered) * 0.95) - 1)]


def main() -> int:
    """跑一轮压测并落 baseline.json；超预算或比上次明显退化时返回 1。"""
    old = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    with tempfile.TemporaryDirectory() as raw:
        prepare(Path(raw))
        samples = []
        result = {}
        for _ in range(RUNS):
            start = time.perf_counter()
            result = exec_pool.run_blocking(db.exec_sql, SQL)
            samples.append(time.perf_counter() - start)
        assert result["row_count"] == 1000, f"聚合结果行数不对：{result['row_count']}"

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": ROWS,
        "runs": RUNS,
        "sql": SQL,
        "p50_s": round(statistics.median(samples), 4),
        "p95_s": round(p95(samples), 4),
        "budget_s": BUDGET_S,
        "max_workers": config.EXEC_MAX_WORKERS,
        "cpu_count": os.cpu_count(),
        "previous_p95_s": old.get("p95_s"),
    }
    BASELINE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    if report["p95_s"] > BUDGET_S:
        print(f"退化：p95 {report['p95_s']}s 超过预算 {BUDGET_S}s")
        return 1
    previous = old.get("p95_s")
    if previous and report["p95_s"] > float(previous) * REGRESSION_RATIO:
        print(f"退化：p95 {report['p95_s']}s 超过上次 {previous}s 的 {REGRESSION_RATIO} 倍")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
