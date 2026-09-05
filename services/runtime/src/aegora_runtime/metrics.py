from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest


REQUESTS_TOTAL = Counter(
    "aegora_runtime_http_requests_total",
    "Total HTTP requests handled by the runner API.",
    ["method", "path", "status"],
)
REQUEST_LATENCY_SECONDS = Histogram(
    "aegora_runtime_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
REQUESTS_IN_PROGRESS = Gauge(
    "aegora_runtime_http_requests_in_progress",
    "HTTP requests currently in progress.",
    ["method", "path"],
)
RUNTIME_INVALIDATION_EVENTS_TOTAL = Counter(
    "aegora_runtime_invalidation_events_total",
    "Runtime invalidation events processed by outcome and event type.",
    ["event_type", "outcome"],
)
RUNTIME_INVALIDATION_EVENT_LAG_SECONDS = Histogram(
    "aegora_runtime_invalidation_event_lag_seconds",
    "Delay between control-plane event occurrence and runtime consumption.",
    ["event_type"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300),
)
RUNTIME_INVALIDATION_SUBSCRIBER_ERRORS_TOTAL = Counter(
    "aegora_runtime_invalidation_subscriber_errors_total",
    "Redis invalidation subscriber loop failures that trigger reconnect backoff.",
)


def instrument_fastapi(app: FastAPI) -> None:
    if getattr(app.state, "prometheus_instrumented", False):
        return
    app.state.prometheus_instrumented = True

    @app.middleware("http")
    async def prometheus_http_metrics(request: Request, call_next: Any) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        method = request.method
        path = route_template(request)
        REQUESTS_IN_PROGRESS.labels(method, path).inc()
        started = time.perf_counter()
        status = "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            path = route_template(request)
            return response
        except Exception:
            status = "500"
            raise
        finally:
            elapsed = time.perf_counter() - started
            REQUESTS_IN_PROGRESS.labels(method, path).dec()
            REQUESTS_TOTAL.labels(method, path, status).inc()
            REQUEST_LATENCY_SECONDS.labels(method, path, status).observe(elapsed)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def record_runtime_invalidation_event(event_type: str, outcome: str, lag_seconds: float | None = None) -> None:
    event_label = event_type or "unknown"
    RUNTIME_INVALIDATION_EVENTS_TOTAL.labels(event_label, outcome).inc()
    if lag_seconds is not None:
        RUNTIME_INVALIDATION_EVENT_LAG_SECONDS.labels(event_label).observe(max(0.0, lag_seconds))


def record_runtime_invalidation_subscriber_error() -> None:
    RUNTIME_INVALIDATION_SUBSCRIBER_ERRORS_TOTAL.inc()


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path or request.url.path)
