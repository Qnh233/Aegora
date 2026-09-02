#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import os

import pocoflow.ui.monitor

from aegora_runtime.config import load_settings


def main() -> None:
    settings = load_settings()
    monitor_path = pocoflow.ui.monitor.__file__
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        monitor_path,
        "--server.address",
        "127.0.0.1",
        "--server.port",
        "8501",
        "--",
        str(settings.observability.pocoflow_db_path),
    ]
    env = os.environ.copy()
    env.setdefault("STREAMLIT_SERVER_HEADLESS", "true")
    env.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")
    raise SystemExit(subprocess.call(command, env=env))


if __name__ == "__main__":
    main()
