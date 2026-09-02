#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable
from typing import Any

from aegora_runtime.config import Settings, load_settings
from aegora_runtime.data_ops.embedding_jobs import embed_pending
from aegora_runtime.data_ops.locks import advisory_job_lock
from aegora_runtime.data_ops.reflection_flow import run_weekly_reflection
from aegora_runtime.data_ops.reports import DataOpsReport, save_report
from aegora_runtime.data_ops.scheduler import DataOpsScheduler, scheduled_job
from aegora_runtime.data_ops.sync_content import sync_content
from aegora_runtime.logging import get_logger, log_event, setup_logging


LOGGER = get_logger("data_ops")


def main() -> None:
    parser = argparse.ArgumentParser(description="Aegora Runtime data operations service.")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_cmd = sub.add_parser("sync-content", help="Sync Strapi FAQ/Skill content into PG retrieval copies.")
    sync_cmd.add_argument("--content", choices=["all", "faq", "skills"], default="all")

    embed_cmd = sub.add_parser("embed-pending", help="Generate pending FAQ/Skill embeddings.")
    embed_cmd.add_argument("--content", choices=["all", "faq", "skills"], default="all")
    embed_cmd.add_argument("--retry-failed", action="store_true")
    embed_cmd.add_argument("--limit", type=int)

    reflect_cmd = sub.add_parser("weekly-reflection", help="Analyze recent conversations and write reports/drafts.")
    reflect_cmd.add_argument("--days", type=int, default=env_int("DATA_OPS_REFLECTION_DAYS", 7))
    reflect_cmd.add_argument("--dry-run", action="store_true")
    reflect_cmd.add_argument("--write-skill-drafts", action="store_true", default=env_bool("DATA_OPS_WRITE_SKILL_DRAFTS", True))
    reflect_cmd.add_argument("--no-write-skill-drafts", action="store_false", dest="write_skill_drafts")
    reflect_cmd.add_argument("--min-cluster-size", type=int, default=env_int("DATA_OPS_MIN_CLUSTER_SIZE", 3))
    reflect_cmd.add_argument("--min-negative-feedback", type=int, default=env_int("DATA_OPS_MIN_NEGATIVE_FEEDBACK", 1))

    sub.add_parser("scheduler", help="Run internal scheduler loop.")
    args = parser.parse_args()

    settings = load_settings(validate_secrets=True)
    setup_logging(settings, log_dir=settings.observability.log_dir)

    if args.command == "sync-content":
        run_sync_content(settings, args.content)
    elif args.command == "embed-pending":
        run_embed_pending(settings, args.content, retry_failed=args.retry_failed, limit=args.limit)
    elif args.command == "weekly-reflection":
        run_reflection(
            settings,
            days=args.days,
            dry_run=args.dry_run,
            write_skill_drafts=args.write_skill_drafts,
            min_cluster_size=args.min_cluster_size,
            min_negative_feedback=args.min_negative_feedback,
        )
    elif args.command == "scheduler":
        run_scheduler(settings)


def run_sync_content(settings: Settings, content: str = "all") -> DataOpsReport:
    return run_reported_job(
        settings,
        "sync_content",
        lambda: sync_content(settings, content),
        summary_prefix=f"sync-content content={content}",
    )


def run_embed_pending(
    settings: Settings,
    content: str = "all",
    *,
    retry_failed: bool = False,
    limit: int | None = None,
) -> DataOpsReport:
    return run_reported_job(
        settings,
        "embed_pending",
        lambda: embed_pending(settings, content, retry_failed=retry_failed, limit=limit),
        summary_prefix=f"embed-pending content={content} retry_failed={retry_failed}",
    )


def run_reflection(
    settings: Settings,
    *,
    days: int,
    dry_run: bool,
    write_skill_drafts: bool,
    min_cluster_size: int,
    min_negative_feedback: int,
) -> DataOpsReport:
    def task() -> dict[str, Any]:
        result = run_weekly_reflection(
            settings,
            days=days,
            dry_run=dry_run,
            write_drafts=write_skill_drafts,
            min_cluster_size=min_cluster_size,
            min_negative_feedback=min_negative_feedback,
        )
        return {
            "metrics": result.metrics,
            "items": result.items,
            "created_skill_draft_ids": result.created_skill_draft_ids,
            "period_start": result.period_start,
            "period_end": result.period_end,
        }

    return run_reported_job(
        settings,
        "weekly_reflection",
        task,
        summary_prefix=f"weekly-reflection days={days} dry_run={dry_run}",
    )


def run_reported_job(
    settings: Settings,
    job_type: str,
    task: Callable[[], dict[str, Any]],
    *,
    summary_prefix: str,
) -> DataOpsReport:
    # ponytail: one global data-ops lock; split locks only if these jobs need real parallelism.
    with advisory_job_lock(settings, "global") as acquired:
        if not acquired:
            report = DataOpsReport(
                job_type=job_type,
                status="skipped",
                summary=f"{summary_prefix} skipped: lock not acquired",
                metrics={"lock_acquired": False},
            )
            save_report(settings, report)
            log_report(report)
            return report
        try:
            payload = task()
            metrics = payload.get("metrics", payload)
            report = DataOpsReport(
                job_type=job_type,
                status="completed",
                summary=f"{summary_prefix} completed",
                metrics=metrics,
                items=payload.get("items", []),
                created_skill_draft_ids=payload.get("created_skill_draft_ids", []),
                period_start=payload.get("period_start"),
                period_end=payload.get("period_end"),
            )
        except Exception as exc:
            report = DataOpsReport(
                job_type=job_type,
                status="failed",
                summary=f"{summary_prefix} failed",
                metrics={"lock_acquired": True},
                error=f"{type(exc).__name__}: {exc}"[:4000],
            )
            save_report(settings, report)
            log_report(report, level=logging.ERROR)
            raise
        save_report(settings, report)
        log_report(report)
        return report


def log_report(report: DataOpsReport, *, level: int = logging.INFO) -> None:
    log_event(
        LOGGER,
        level,
        "data_ops_job_finished",
        job_id=report.job_id,
        job_type=report.job_type,
        status=report.status,
        summary=report.summary,
        metrics=report.metrics,
        item_count=len(report.items),
        created_skill_draft_ids=report.created_skill_draft_ids,
        error=report.error,
    )


def run_scheduler(settings: Settings) -> None:
    sleep_seconds = env_int("DATA_OPS_LOOP_SLEEP_SECONDS", 30)
    sync_minutes = env_int("DATA_OPS_SYNC_INTERVAL_MINUTES", 60)
    embed_minutes = env_int("DATA_OPS_EMBED_INTERVAL_MINUTES", 60)
    reflection_hours = env_int("DATA_OPS_REFLECTION_INTERVAL_HOURS", 168)
    scheduler = DataOpsScheduler(
        [
            scheduled_job("sync_content", sync_minutes * 60, lambda: run_sync_content(settings, "all")),
            scheduled_job("embed_pending", embed_minutes * 60, lambda: run_embed_pending(settings, "all")),
            scheduled_job(
                "weekly_reflection",
                reflection_hours * 3600,
                lambda: run_reflection(
                    settings,
                    days=env_int("DATA_OPS_REFLECTION_DAYS", 7),
                    dry_run=env_bool("DATA_OPS_REFLECTION_DRY_RUN", False),
                    write_skill_drafts=env_bool("DATA_OPS_WRITE_SKILL_DRAFTS", True),
                    min_cluster_size=env_int("DATA_OPS_MIN_CLUSTER_SIZE", 3),
                    min_negative_feedback=env_int("DATA_OPS_MIN_NEGATIVE_FEEDBACK", 1),
                ),
            ),
        ],
        sleep_seconds=sleep_seconds,
    )
    log_event(
        LOGGER,
        logging.INFO,
        "data_ops_scheduler_started",
        sleep_seconds=sleep_seconds,
        sync_minutes=sync_minutes,
        embed_minutes=embed_minutes,
        reflection_hours=reflection_hours,
    )
    scheduler.run_forever()


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


if __name__ == "__main__":
    main()
