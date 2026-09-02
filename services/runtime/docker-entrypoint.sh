#!/bin/sh
set -eu

case "${APP_MODE:-api}" in
    api)
        exec uvicorn apps.api_app:app --host "${API_HOST:-0.0.0.0}" --port "${API_PORT:-5000}" --workers "${API_WORKERS:-1}"
        ;;
    gradio)
        exec python apps/chat_app.py
        ;;
    wecom)
        exec python apps/wecom_aibot_app.py
        ;;
    data_ops)
        exec python apps/data_ops_app.py scheduler
        ;;
    *)
        echo "Unsupported APP_MODE=${APP_MODE}. Use api, gradio, wecom, or data_ops." >&2
        exit 2
        ;;
esac
