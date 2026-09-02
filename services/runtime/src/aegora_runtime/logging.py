from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pocoflow.logging import setup_logging as setup_pocoflow_logging

from aegora_runtime.config import Settings, load_settings


APP_LOGGER_NAME = "aegora_runtime"


def setup_logging(
    settings: Settings | None = None,
    *,
    log_dir: str | Path = "logs",
    console: bool = False,
) -> Path | None:
    cfg = settings or load_settings()
    level = getattr(logging, cfg.app.log_level.upper(), logging.INFO)
    console_enabled = console or _env_flag("APP_CONSOLE_LOG_ENABLED", default=False)

    logger = logging.getLogger(APP_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if not console_enabled:
        for handler in list(logger.handlers):
            if _is_console_handler(handler):
                logger.removeHandler(handler)
                handler.close()

    if console_enabled and not any(_is_console_handler(handler) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)

    if not cfg.observability.file_log_enabled:
        if not logger.handlers:
            logger.addHandler(logging.NullHandler())
        return None

    # PocoFlow has its own logger namespace. Initialise it once at app start.
    log_path = setup_pocoflow_logging(
        "aegora_runtime",
        log_level=cfg.app.log_level.lower(),
        log_dir=log_dir,
        console=console,
    )
    resolved_path = log_path.resolve()
    has_file_handler = any(
        isinstance(handler, logging.FileHandler)
        and Path(handler.baseFilename).resolve() == resolved_path
        for handler in logger.handlers
    )
    if not has_file_handler:
        file_handler = logging.FileHandler(resolved_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(file_handler)
    return log_path


def _is_console_handler(handler: logging.Handler) -> bool:
    return (
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, (logging.FileHandler, logging.NullHandler))
    )


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{APP_LOGGER_NAME}.{name}")


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(
        level,
        json.dumps(
            {"event": event, "instance_id": os.environ.get("INSTANCE_ID", "local"), **fields},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
