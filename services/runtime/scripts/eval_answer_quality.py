#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegora_runtime.agent_loop import AgentRequest, run_agent
from aegora_runtime.config import Settings, load_settings
from aegora_runtime.deepseek import ChatMessage, DeepSeekClient, DeepSeekError
from aegora_runtime.demo_agent import build_demo_dependencies
from aegora_runtime.real_agent import build_real_dependencies


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "evals" / "eval_core_100.jsonl"
DEFAULT_OUTPUT = ROOT / "evals" / "latest_answer_quality_eval.json"
DEFAULT_CACHE = ROOT / "evals" / "cache" / "answer_quality_cache.jsonl"
JUDGE_VERSION = "answer_quality_judge_v3"


@dataclass
class QualityStats:
    dataset: str
    total: int = 0
    answer_scored: int = 0
    judged: int = 0
    grounded: int = 0
    hallucinated: int = 0
    key_points_total: int = 0
    key_points_covered: int = 0
    answer_quality_score_sum: float = 0.0
    answer_quality_pass: int = 0
    low_quality: int = 0
    judge_errors: int = 0
    skipped: int = 0
    samples: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "total": self.total,
            "answer_scored": self.answer_scored,
            "judged": self.judged,
            "grounded_rate": pct(self.grounded, self.judged),
            "hallucination_rate": pct(self.hallucinated, self.judged),
            "key_step_coverage": pct(self.key_points_covered, self.key_points_total),
            "answer_quality_score_avg": round(self.answer_quality_score_sum / self.judged, 4) if self.judged else None,
            "answer_quality_pass_rate": pct(self.answer_quality_pass, self.judged),
            "low_quality_rate": pct(self.low_quality, self.judged),
            "judge_errors": self.judge_errors,
            "skipped": self.skipped,
            "cache": getattr(self, "cache_summary", {}),
            "thresholds": {
                "grounded_rate": ">= 0.97",
                "hallucination_rate": "<= 0.03",
                "key_step_coverage": ">= 0.90",
                "answer_quality_score_avg": ">= 4.20",
                "answer_quality_pass_rate": ">= 0.90",
                "low_quality_rate": "<= 0.03",
            },
            "samples": self.samples[:20],
        }


@dataclass(frozen=True)
class EvalOutcome:
    result: dict[str, Any]
    judgement: dict[str, Any] | None = None
    skipped: bool = False
    judge_error: str | None = None
    cache_hit: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate answer quality with retrieved FAQ/tool evidence as source of truth.")
    parser.add_argument("dataset", nargs="?", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--agent", choices=["demo", "real"], default="real")
    parser.add_argument("--loop-mode", choices=["planner"], default=None)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--cache-path", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--include-details", action="store_true")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    deps = build_real_dependencies(settings) if args.agent == "real" else build_demo_dependencies(settings)
    judge = DeepSeekAnswerJudge(settings)
    cache = None if args.no_cache else JsonlCache(args.cache_path)

    rows = read_jsonl(args.dataset)
    if args.limit:
        rows = rows[: args.limit]

    stats = evaluate_rows(
        f"{args.agent}:{args.loop_mode or settings.agent.loop_mode}:{args.dataset.name}",
        rows,
        settings,
        deps,
        judge,
        args.loop_mode,
        agent_name=args.agent,
        concurrency=args.concurrency,
        cache=cache,
        include_details=args.include_details,
    )
    payload = stats.as_dict()
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"wrote={args.output}")


class DeepSeekAnswerJudge:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = DeepSeekClient(settings.deepseek)

    def judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.client.chat_json(
            [
                ChatMessage("system", JUDGE_SYSTEM),
                ChatMessage("user", json.dumps(payload, ensure_ascii=False)),
            ],
            model=self.settings.deepseek.fast_model,
            temperature=0.0,
        )


class JsonlCache:
    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.read_count = 0
        self.write_count = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                item = json.loads(line)
                key = item.get("key")
                if isinstance(key, str):
                    self._items[key] = item

    def get(self, key: str) -> dict[str, Any] | None:
        item = self._items.get(key)
        if item:
            self.read_count += 1
        return item

    def put(self, item: dict[str, Any]) -> None:
        key = item["key"]
        with self._lock:
            if key in self._items:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
            self._items[key] = item
            self.write_count += 1


def evaluate_rows(
    name: str,
    rows: list[dict[str, Any]],
    settings: Settings,
    deps,
    judge: DeepSeekAnswerJudge,
    loop_mode: str | None = None,
    *,
    agent_name: str = "real",
    concurrency: int = 1,
    cache: JsonlCache | None = None,
    include_details: bool = False,
) -> QualityStats:
    stats = QualityStats(dataset=name)
    stats.total = len(rows)
    if concurrency <= 1:
        for index, row in enumerate(rows, start=1):
            outcome = evaluate_case(row, settings, deps, judge, loop_mode, agent_name, cache)
            apply_outcome(stats, row, outcome, include_details)
            print_progress(index, len(rows), row, outcome)
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {
                executor.submit(evaluate_case, row, settings, deps, judge, loop_mode, agent_name, cache): row
                for row in rows
            }
            for index, future in enumerate(as_completed(futures), start=1):
                row = futures[future]
                outcome = future.result()
                apply_outcome(stats, row, outcome, include_details)
                print_progress(index, len(rows), row, outcome)
    if cache:
        stats.cache_summary = {
            "path": str(cache.path),
            "hits": cache.read_count,
            "writes": cache.write_count,
        }
    return stats


def evaluate_case(
    row: dict[str, Any],
    settings: Settings,
    deps,
    judge: DeepSeekAnswerJudge,
    loop_mode: str | None,
    agent_name: str,
    cache: JsonlCache | None,
) -> EvalOutcome:
    key = make_cache_key(row, agent_name, loop_mode or settings.agent.loop_mode)
    cached = cache.get(key) if cache else None
    if cached:
        return EvalOutcome(
            result=cached["result"],
            judgement=cached.get("judgement"),
            skipped=bool(cached.get("skipped")),
            judge_error=cached.get("judge_error"),
            cache_hit=True,
        )

    result = compact_result(run_one(row, settings, deps, loop_mode))
    if not should_score(result):
        outcome = EvalOutcome(result=result, skipped=True)
    else:
        payload = build_judge_payload(row, result)
        if not payload["evidence"]["retrieved_faqs"] and not payload["evidence"]["tool_observations"]:
            outcome = EvalOutcome(result=result, judgement=deterministic_no_evidence_judgement(payload))
        else:
            try:
                judgement = normalize_judgement(judge.judge(payload), len(payload["expected_key_points"]))
                outcome = EvalOutcome(result=result, judgement=judgement)
            except (DeepSeekError, ValueError, TypeError) as exc:
                outcome = EvalOutcome(result=result, judge_error=f"{type(exc).__name__}: {exc}")

    if cache:
        cache.put(
            {
                "key": key,
                "judge_version": JUDGE_VERSION,
                "case_id": row.get("id"),
                "result": outcome.result,
                "judgement": outcome.judgement,
                "skipped": outcome.skipped,
                "judge_error": outcome.judge_error,
            }
        )
    return outcome


def apply_outcome(
    stats: QualityStats,
    row: dict[str, Any],
    outcome: EvalOutcome,
    include_details: bool,
) -> None:
    if outcome.skipped:
        stats.skipped += 1
        return
    stats.answer_scored += 1
    if outcome.judge_error:
        stats.judge_errors += 1
        add_sample(stats, row, outcome.result, {"judge_error": outcome.judge_error}, include_details)
        return
    if outcome.judgement is None:
        stats.judge_errors += 1
        add_sample(stats, row, outcome.result, {"judge_error": "missing_judgement"}, include_details)
        return
    apply_judgement(stats, row, outcome.result, outcome.judgement, include_details)


def run_one(row: dict[str, Any], settings: Settings, deps, loop_mode: str | None) -> dict[str, Any]:
    try:
        return run_agent(
            AgentRequest(
                query=row["query"],
                session_id=f"answer-quality-{row['id']}",
                history=row.get("history") or [],
            ),
            deps,
            settings,
            loop_mode=loop_mode,
            attach_hooks=False,
            enable_pocoflow_db=False,
        )
    except Exception as exc:
        return {
            "status": "error",
            "route": None,
            "answer": "",
            "retrieved_faqs": [],
            "tool_observations": [],
            "decision": {"reason": f"{type(exc).__name__}: {exc}"},
        }


def compact_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "route": result.get("route"),
        "answer": result.get("answer"),
        "retrieved_faqs": [
            {
                "faq_id": item.get("faq_id"),
                "title": item.get("title"),
                "response": item.get("response"),
                "source_ranks": item.get("source_ranks"),
            }
            for item in (result.get("retrieved_faqs") or [])[:5]
        ],
        "tool_observations": result.get("tool_observations") or [],
        "decision": result.get("decision") or {},
    }


def should_score(result: dict[str, Any]) -> bool:
    if not result.get("answer"):
        return False
    if result.get("route") == "faq_answer":
        return True
    return has_substantive_tool_evidence(result)


def has_substantive_tool_evidence(result: dict[str, Any]) -> bool:
    evidence_tools = {"lookup_faq_detail"}
    for item in result.get("tool_observations") or []:
        tool_name = item.get("tool_name") or item.get("name")
        if tool_name in evidence_tools and item.get("status") == "ok":
            return True
    return False


def build_judge_payload(row: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": row.get("id"),
        "query": row.get("query"),
        "answer": result.get("answer") or "",
        "route": result.get("route"),
        "expected_key_points": [str(item) for item in row.get("must_include") or []],
        "evidence": {
            "retrieved_faqs": [
                {
                    "faq_id": item.get("faq_id"),
                    "title": item.get("title"),
                    "response": clip(str(item.get("response") or ""), 2400),
                }
                for item in (result.get("retrieved_faqs") or [])[:5]
            ],
            "tool_observations": [
                {
                    "tool_name": item.get("tool_name") or item.get("name"),
                    "status": item.get("status"),
                    "output": item.get("output"),
                    "error": item.get("error"),
                }
                for item in (result.get("tool_observations") or [])[:5]
            ],
        },
    }


def deterministic_no_evidence_judgement(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "grounded": False,
        "hallucination": True,
        "key_points_total": len(payload.get("expected_key_points") or []),
        "key_points_covered": 0,
        "answer_quality_score": 1,
        "missing_key_points": payload.get("expected_key_points") or [],
        "unsupported_claims": ["answer_without_retrieved_faq_or_tool_evidence"],
        "risk_level": "high",
        "reason": "faq/tool answer has no evidence",
    }


def normalize_judgement(data: dict[str, Any], expected_key_point_count: int) -> dict[str, Any]:
    total = int(data.get("key_points_total") if data.get("key_points_total") is not None else expected_key_point_count)
    covered = int(data.get("key_points_covered") if data.get("key_points_covered") is not None else 0)
    answer_quality_score = float(data.get("answer_quality_score") if data.get("answer_quality_score") is not None else 1)
    if total < 0 or covered < 0 or covered > total:
        raise ValueError("invalid key point counts in judge result")
    if answer_quality_score < 1 or answer_quality_score > 5:
        raise ValueError("answer_quality_score must be between 1 and 5")
    return {
        "grounded": bool(data.get("grounded")),
        "hallucination": bool(data.get("hallucination")),
        "key_points_total": total,
        "key_points_covered": covered,
        "answer_quality_score": answer_quality_score,
        "missing_key_points": list(data.get("missing_key_points") or []),
        "unsupported_claims": list(data.get("unsupported_claims") or []),
        "risk_level": str(data.get("risk_level") or "unknown"),
        "reason": str(data.get("reason") or ""),
    }


def apply_judgement(
    stats: QualityStats,
    row: dict[str, Any],
    result: dict[str, Any],
    judgement: dict[str, Any],
    include_details: bool,
) -> None:
    stats.judged += 1
    if judgement["grounded"]:
        stats.grounded += 1
    if judgement["hallucination"]:
        stats.hallucinated += 1
    stats.key_points_total += int(judgement["key_points_total"])
    stats.key_points_covered += int(judgement["key_points_covered"])
    answer_quality_score = float(judgement["answer_quality_score"])
    stats.answer_quality_score_sum += answer_quality_score
    if answer_quality_score >= 4:
        stats.answer_quality_pass += 1
    if answer_quality_score <= 2:
        stats.low_quality += 1
    if not judgement["grounded"] or judgement["hallucination"] or judgement["missing_key_points"] or answer_quality_score < 4:
        add_sample(stats, row, result, judgement, include_details)


def add_sample(
    stats: QualityStats,
    row: dict[str, Any],
    result: dict[str, Any],
    judgement: dict[str, Any],
    include_details: bool,
) -> None:
    if len(stats.samples) >= 20:
        return
    sample = {
        "id": row.get("id"),
        "query": clip(str(row.get("query") or ""), 120),
        "route": result.get("route"),
        "answer": clip(str(result.get("answer") or ""), 220),
        "judgement": judgement,
    }
    if include_details:
        sample["retrieved_faq_ids"] = [item.get("faq_id") for item in result.get("retrieved_faqs") or []]
        sample["expected_key_points"] = row.get("must_include") or []
    stats.samples.append(sample)


def pct(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 4)


def clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "..."


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def make_cache_key(row: dict[str, Any], agent_name: str, loop_mode: str) -> str:
    payload = {
        "judge_version": JUDGE_VERSION,
        "agent": agent_name,
        "loop_mode": loop_mode,
        "case_id": row.get("id"),
        "query": row.get("query"),
        "history": row.get("history") or [],
        "must_include": row.get("must_include") or [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def print_progress(done: int, total: int, row: dict[str, Any], outcome: EvalOutcome) -> None:
    if outcome.cache_hit:
        status = "cache"
    elif outcome.skipped:
        status = "skip"
    elif outcome.judge_error:
        status = "judge_error"
    elif outcome.judgement:
        status = "judged"
    else:
        status = "unknown"
    print(f"[{done}/{total}] {status} id={row.get('id')} route={outcome.result.get('route')}", file=sys.stderr, flush=True)


JUDGE_SYSTEM = """你是客服回答质量评估器，只输出 JSON 对象。
你必须把 evidence.retrieved_faqs 和 evidence.tool_observations 当作唯一事实来源。

评估目标：
1. grounded: 回答中的产品事实、路径、按钮、限制条件、链接、时间、金额、处理状态，是否均可由证据支持。
2. hallucination: 回答是否存在证据不支持且可能误导用户的事实、路径、承诺或处理结果。
3. key_points_total/key_points_covered: expected_key_points 中有多少关键点被答案语义覆盖；不是逐字匹配。
4. answer_quality_score: 1 到 5 分，评估“作为客服回答是否好用”。

判定规则：
- 允许自然语言改写、压缩和顺序调整。
- 如果答案说了证据没有的具体路径、按钮、限制条件、链接、处理状态，grounded=false。
- 如果无据内容会误导用户操作或承诺业务结果，hallucination=true。
- 如果只是礼貌语、过渡语、让用户补充信息，不算幻觉。
- key_points_total 必须等于输入 expected_key_points 数量；没有关键点时为 0。
- answer_quality_score 必须综合准确性、完整性、清晰度、自然度、可执行性；如果 grounded=false 或 hallucination=true，最高 3 分；如果存在高风险幻觉，最高 2 分。

answer_quality_score 评分：
- 5: 准确有据，关键步骤完整，结构清晰，语气自然，用户可直接照做。
- 4: 准确有据，基本完整可用，但表达或结构有小瑕疵。
- 3: 大体可用，但有明显遗漏、表达不清、或轻微无据内容。
- 2: 答非所问、遗漏严重、或存在会影响用户操作的无据内容。
- 1: 不可用、严重误导、严重无依据、或完全没有回答问题。

输出字段：
{
  "grounded": true/false,
  "hallucination": true/false,
  "key_points_total": number,
  "key_points_covered": number,
  "answer_quality_score": 1-5,
  "missing_key_points": [string],
  "unsupported_claims": [string],
  "risk_level": "none" | "low" | "medium" | "high",
  "reason": "简短中文理由"
}
"""


if __name__ == "__main__":
    main()
