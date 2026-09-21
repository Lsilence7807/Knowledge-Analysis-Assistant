# 文件：backend/app/services/evalharness.py
# 作用：评测跑批：golden 集逐条走「摄取 → 转 SQL → 执行 → 结论」，算通过率 / fidelity / 降级率与成本，并与基线对比
# 阶段：F8 观测与评测（兼 P16；ADR-18 废止：用例以 deepeval 数据集表示，基线对比 exit 1 不变）
# 依赖：标准库 argparse/json/logging/re/time/sys、deepeval（可选）、backend/app/core/{config,db}、
#       backend/app/services/{agent,ingest,insight,llm,llm_models,nlu,store,trace}
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

from app.core import config, db
from app.services import agent, ingest, insight, llm, llm_models, nlu, store, trace

logger = logging.getLogger(__name__)

# §4.10 的四个指标：前两个越高越好，后两个越低越好
METRICS = ("sql_pass_rate", "fidelity_rate", "degrade_rate", "cost_per_task")
_HIGHER_IS_BETTER = {"sql_pass_rate", "fidelity_rate"}
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _resolve(path) -> Path:
    """相对路径按项目根解析（与 core/config 的 BASE_DIR 同口径，cwd 无所谓）。"""
    candidate = Path(str(path))
    return candidate if candidate.is_absolute() else config.BASE_DIR / candidate


def load_cases(golden) -> list[dict]:
    """读 golden.jsonl（§4.10 一行一例）；坏行直接报错——评测集坏掉比跑出一片假绿更危险。"""
    path = _resolve(golden)
    cases: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} 第 {lineno} 行不是合法 JSON：{exc}") from exc
        if not case.get("id") or not case.get("question"):
            raise ValueError(f"{path.name} 第 {lineno} 行缺 id 或 question")
        cases.append(case)
    return cases


def build_dataset(cases: list[dict]):
    """把用例装成 deepeval 的 EvaluationDataset（原字段放 additional_metadata）。

    deepeval 缺席或版本不认时回退成原列表：跑批逻辑不该被评测库的版本差异绊住。
    """
    try:
        from deepeval.dataset import EvaluationDataset, Golden

        return EvaluationDataset(goldens=[Golden(input=case["question"], additional_metadata=case) for case in cases])
    except Exception as exc:  # noqa: BLE001 - 表示形式不是跑批的硬依赖
        logger.warning("deepeval 数据集不可用，按原始用例跑：%s", exc)
        return cases


def _fidelity(summary_obj, result: dict) -> bool:
    """结论里的数字都能回溯到结果表（§4.10 的 fidelity 口径）。

    口径只有一处实现：insight.unsupported_numbers（P4 的 caveats 用的是同一条），
    这里不另写一套比对——两套判定迟早会打架（第一版按字面比对，模型把 133116.29 说成 13.3 万就被误判）。
    """
    return not insight.unsupported_numbers(summary_obj, result)


def _ask(question: str, dataset_id: str) -> dict:
    """走与 /ask 同一条链路：默认 agent 多步，ENABLE_AGENT=false 时落到 P3 单跳。"""
    dataset = store.get_dataset(dataset_id) or {}
    if config.ENABLE_AGENT:
        return asdict(agent.run(question, None, dataset_id))
    profile = {**(dataset.get("profile") or {}), "table": dataset.get("table_name")}
    result = db.exec_sql(nlu.to_sql(question, profile, []))
    return {"sql": result["sql"], "columns": result["columns"], "rows": result["rows"], "degraded": []}


def _insight(question: str, body: dict, dataset: dict):
    """跑一次结论，回 Insight 对象；结论起不来给 None（这一条的 insight 与 fidelity 项算失败）。"""
    profile = {**(dataset.get("profile") or {}), "table": dataset.get("table")}
    result = {"columns": body.get("columns") or [], "rows": body.get("rows") or []}
    try:
        summary_obj = insight.summarize(question, result, profile, [])
    except Exception as exc:  # noqa: BLE001 - 结论失败只影响这一条的判分
        logger.warning("评测用例出结论失败：%s", exc)
        return None
    return summary_obj


def _contains(sql: str, needles: list[str]) -> bool:
    """SQL 里是否含全部关键词（大小写不敏感；期望写松一点，别把模型换说法判死）。"""
    if not needles:
        return True  # 没写期望就是没约束
    lowered = (sql or "").lower()
    return all(str(needle).lower() in lowered for needle in needles)


def _deepeval_faithfulness(question: str, answer: str, contexts: list[str]) -> float | None:
    """有可用模型时用 deepeval 的 FaithfulnessMetric 复算一次忠实度；没模型直接给 None（本地口径为准）。"""
    try:
        llm.ensure_ready()
    except llm.LLMUnavailable:
        return None
    try:
        from deepeval.metrics import FaithfulnessMetric
        from deepeval.models import LiteLLMModel
        from deepeval.test_case import LLMTestCase

        profile = llm_models.resolve(None)
        model = LiteLLMModel(
            model=llm_models.litellm_model(profile),
            api_key=config.api_key(profile),
            base_url=profile.get("base_url"),
        )
        case = LLMTestCase(input=question, actual_output=answer or "（无结论）", retrieval_context=contexts or [""])
        metric = FaithfulnessMetric(threshold=0.95, model=model, async_mode=False)
        metric.measure(case)
        return float(metric.score)
    except Exception as exc:  # noqa: BLE001 - 这是交叉校验，失败不拦跑批
        logger.warning("deepeval faithfulness 失败，本轮以本地口径为准：%s", exc)
        return None


def _run_case(case: dict) -> dict:
    """跑一条用例并逐项判分；单条炸掉只算这一条失败，不中止整轮。"""
    started = time.perf_counter()
    task_id = f"eval_{case['id']}"
    trace.bind(task_id)
    outcome = {
        "id": case["id"],
        "task_id": task_id,
        "question": case["question"],
        "sql": "",
        "summary": "",
        "ok": False,
        "columns_ok": False,
        "insight_ok": False,
        "phrases_ok": False,
        "fidelity": False,
        "degraded": [],
        "cost": 0.0,
        "ms": 0,
        "deepeval_faithfulness": None,
        "error": "",
    }
    try:
        # ingest 按 name 的后缀判格式，name 必须带扩展名（第一版漏了，三条用例全被判「不支持的文件」）
        fixture = _resolve(case["fixture"])
        dataset = ingest.ingest_file(fixture, f"eval-{case['id']}{fixture.suffix}")
        body = _ask(case["question"], dataset["dataset_id"])
        summary_obj = _insight(case["question"], body, dataset)
    except Exception as exc:  # noqa: BLE001 - 缺 fixture、模型炸、SQL 被拒都算这条失败
        outcome["error"] = str(exc)[:200]
        outcome["ms"] = trace.elapsed_ms(started)
        return outcome

    if summary_obj is None:
        outcome["error"] = "结论没跑出来（模型不可用或输出不合契约）"
        outcome["ms"] = trace.elapsed_ms(started)
        return outcome
    columns = [str(name) for name in body.get("columns") or []]
    outcome["sql"] = body.get("sql") or ""
    outcome["degraded"] = list(body.get("degraded") or [])
    sql_ok = _contains(outcome["sql"], case.get("expect_sql_contains") or [])
    outcome["columns_ok"] = _contains(" ".join(columns), case.get("expect_columns") or [])
    fields = {key for key, value in summary_obj.model_dump().items() if value}
    summary = summary_obj.summary
    # 结论原文留在用例结果里：fidelity 判红时得能看出是哪个数字没追溯上
    outcome["summary"] = summary
    outcome["insight_ok"] = set(case.get("expect_insight_fields") or []) <= fields
    outcome["phrases_ok"] = not any(str(p) in summary for p in case.get("forbid_phrases") or [])
    outcome["fidelity"] = _fidelity(summary_obj, body)
    outcome["ms"] = trace.elapsed_ms(started)
    outcome["cost"] = round(sum(span["cost"] for span in store.trace_spans(task_id)), 6)
    outcome["deepeval_faithfulness"] = _deepeval_faithfulness(case["question"], summary, columns)
    outcome["ok"] = bool(sql_ok) and outcome["columns_ok"] and outcome["insight_ok"] and outcome["phrases_ok"]
    outcome["ok"] = outcome["ok"] and outcome["fidelity"]
    return outcome


def _aggregate(results: list[dict]) -> dict:
    """把逐条结果压成 §4.10 的四个指标（用例为空时全 0，别造出除零）。"""
    total = len(results)
    divisor = total or 1
    return {
        "total": total,
        "sql_pass_rate": round(sum(1 for item in results if item["sql"]) / divisor, 4),
        "fidelity_rate": round(sum(1 for item in results if item["fidelity"]) / divisor, 4),
        "degrade_rate": round(sum(1 for item in results if item["degraded"]) / divisor, 4),
        "cost_total": round(sum(item["cost"] for item in results), 6),
        "cost_per_task": round(sum(item["cost"] for item in results) / divisor, 6),
    }


def _load_baseline(baseline) -> dict:
    """读基线文件；读不出来就报错——没有基线的「对比」是假绿。"""
    path = _resolve(baseline)
    return json.loads(path.read_text(encoding="utf-8"))


def _compare(metrics: dict, baseline: dict) -> tuple[dict, list[str]]:
    """回 (与基线的差值, 破线的指标名)。基线里没写的指标不参与比较，新增指标不会让老基线永远红。"""
    delta: dict[str, float] = {}
    breached: list[str] = []
    for name in METRICS:
        if name not in baseline:
            continue
        before = float(baseline[name])
        now = float(metrics.get(name) or 0.0)
        delta[name] = round(now - before, 6)
        worse = now < before if name in _HIGHER_IS_BETTER else now > before
        if worse:
            breached.append(name)
    return delta, breached


def run_golden(golden: str, baseline: str | None) -> dict:
    """§4.2 契约：跑一轮 golden 并回指标、基线差值与被破项；顺手落一行 eval_runs。"""
    cases = load_cases(golden)
    dataset = build_dataset(cases)
    results = [_run_case(case) for case in cases]
    metrics = _aggregate(results)
    thresholds = _load_baseline(baseline) if baseline else {}
    delta, breached = _compare(metrics, thresholds)
    run = {
        **metrics,
        "golden_path": str(_resolve(golden)),
        "baseline": thresholds,
        "baseline_delta": delta,
        "breached": breached,
        "dataset_kind": type(dataset).__name__,
        "cases": results,
    }
    try:
        store.insert_eval_run(run)
    except Exception as exc:  # noqa: BLE001 - 落库失败不该让评测结果丢掉
        logger.warning("评测结果落库失败：%s", exc)
    return run


def main(argv: list[str] | None = None) -> int:
    """命令行入口（§4.10 的命令）：低于基线 exit 1，正常 exit 0。"""
    parser = argparse.ArgumentParser(description="跑 golden 集并与基线对比（低于基线 exit 1）")
    parser.add_argument("--golden", default=str(config.EVAL_GOLDEN))
    parser.add_argument("--baseline", default=str(config.EVAL_BASELINE))
    args = parser.parse_args(argv)
    run = run_golden(args.golden, args.baseline)
    print(json.dumps({key: value for key, value in run.items() if key != "cases"}, ensure_ascii=False, indent=2))
    for case in run["cases"]:
        flag = "ok  " if case["ok"] else "fail"
        print(f"  [{flag}] {case['id']} {case['question']}（{case['ms']}ms，成本 {case['cost']}）{case['error']}")
    if run["breached"]:
        print(f"低于基线：{'、'.join(run['breached'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
