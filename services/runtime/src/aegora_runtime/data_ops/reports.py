from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.db import connect


@dataclass
class DataOpsReport:
    job_type: str
    status: str
    summary: str
    metrics: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    created_skill_draft_ids: list[Any] = field(default_factory=list)
    error: str | None = None
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    period_start: datetime | None = None
    period_end: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


def ensure_report_table(settings: Settings) -> None:
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS data_ops_reports (
                    id bigserial PRIMARY KEY,
                    job_id text NOT NULL UNIQUE,
                    job_type text NOT NULL,
                    period_start timestamptz,
                    period_end timestamptz,
                    status text NOT NULL,
                    summary text,
                    metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
                    items jsonb NOT NULL DEFAULT '[]'::jsonb,
                    created_skill_draft_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
                    error text,
                    started_at timestamptz NOT NULL DEFAULT now(),
                    finished_at timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_data_ops_reports_job_type ON data_ops_reports(job_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_data_ops_reports_created_at ON data_ops_reports(created_at)")
        conn.commit()


def save_report(settings: Settings, report: DataOpsReport) -> DataOpsReport:
    ensure_report_table(settings)
    if report.finished_at is None:
        report.finished_at = datetime.now(timezone.utc)
    with connect(settings) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO data_ops_reports (
                    job_id, job_type, period_start, period_end, status, summary,
                    metrics, items, created_skill_draft_ids, error, started_at, finished_at
                )
                VALUES (
                    %(job_id)s, %(job_type)s, %(period_start)s, %(period_end)s, %(status)s, %(summary)s,
                    %(metrics)s::jsonb, %(items)s::jsonb, %(created_skill_draft_ids)s::jsonb,
                    %(error)s, %(started_at)s, %(finished_at)s
                )
                ON CONFLICT (job_id)
                DO UPDATE SET
                    status = excluded.status,
                    summary = excluded.summary,
                    metrics = excluded.metrics,
                    items = excluded.items,
                    created_skill_draft_ids = excluded.created_skill_draft_ids,
                    error = excluded.error,
                    finished_at = excluded.finished_at
                """,
                {
                    "job_id": report.job_id,
                    "job_type": report.job_type,
                    "period_start": report.period_start,
                    "period_end": report.period_end,
                    "status": report.status,
                    "summary": report.summary,
                    "metrics": json.dumps(report.metrics, ensure_ascii=False, sort_keys=True),
                    "items": json.dumps(report.items, ensure_ascii=False, sort_keys=True),
                    "created_skill_draft_ids": json.dumps(
                        report.created_skill_draft_ids, ensure_ascii=False, sort_keys=True
                    ),
                    "error": report.error,
                    "started_at": report.started_at,
                    "finished_at": report.finished_at,
                },
            )
        conn.commit()
    return report
