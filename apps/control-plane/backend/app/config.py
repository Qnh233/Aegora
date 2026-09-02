from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path

DEFAULT_CORS_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
RUNNER_BACKENDS = {"debug", "gateway"}
AUTH_MODES = {"local", "oa"}


@dataclass(frozen=True)
class AppConfig:
    database_url: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str | None = None
    bootstrap_admin_users: tuple[str, ...] = ()
    runner_backend: str = "debug"
    runner_gateway_url: str | None = None
    runner_gateway_timeout_seconds: float = 30.0
    auth_mode: str = "local"
    oa_auth_me_url: str | None = None
    oa_auth_timeout_seconds: float = 3.0
    oa_login_url: str | None = None


def split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def env_path() -> Path | None:
    if configured := os.getenv("AEGORA_ENV_FILE") or os.getenv("ENV_FILE"):
        return Path(configured)
    candidates = [
        # Aegora monorepo canonical configuration.
        Path(__file__).resolve().parents[4] / ".env",
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path(__file__).resolve().parents[2] / ".env",
    ]
    return next((path for path in candidates if path.exists()), None)


def env_value(key: str) -> str:
    path = env_path()
    file_values = parse_env_file(path) if path else {}
    return os.getenv(key) or file_values.get(key, "")


def get_cors_origins() -> tuple[str, ...]:
    origins = split_csv(env_value("CORS_ORIGINS"))
    return origins or DEFAULT_CORS_ORIGINS


def normalize_runner_backend(value: str) -> str:
    backend = (value or "debug").strip().lower()
    if backend not in RUNNER_BACKENDS:
        raise RuntimeError("RUNNER_BACKEND 只支持 debug 或 gateway")
    return backend


def normalize_auth_mode(value: str) -> str:
    mode = (value or "local").strip().lower()
    if mode not in AUTH_MODES:
        raise RuntimeError("AUTH_MODE 只支持 local 或 oa")
    return mode


def parse_timeout_seconds(value: str) -> float:
    if not value:
        return 30.0
    try:
        timeout = float(value)
    except ValueError as error:
        raise RuntimeError("RUNNER_GATEWAY_TIMEOUT_SECONDS 必须是数字") from error
    if timeout <= 0:
        raise RuntimeError("RUNNER_GATEWAY_TIMEOUT_SECONDS 必须大于 0")
    return timeout


def parse_positive_seconds(value: str, default: float, key: str) -> float:
    if not value:
        return default
    try:
        timeout = float(value)
    except ValueError as error:
        raise RuntimeError(f"{key} 必须是数字") from error
    if timeout <= 0:
        raise RuntimeError(f"{key} 必须大于 0")
    return timeout


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    database_url = env_value("DATABASE_URL")
    llm_api_key = env_value("LLM_API_KEY")
    llm_model = env_value("LLM_MODEL")
    bootstrap_admin_users = split_csv(env_value("BOOTSTRAP_ADMIN_USERS"))
    runner_backend = normalize_runner_backend(env_value("RUNNER_BACKEND"))
    runner_gateway_url = env_value("RUNNER_GATEWAY_URL") or None
    auth_mode = normalize_auth_mode(env_value("AUTH_MODE"))
    oa_auth_me_url = env_value("OA_AUTH_ME_URL") or None
    if not database_url:
        raise RuntimeError("DATABASE_URL 未配置")
    if auth_mode == "oa" and not oa_auth_me_url:
        raise RuntimeError("AUTH_MODE=oa 时必须配置 OA_AUTH_ME_URL")
    return AppConfig(
        database_url=database_url,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        llm_base_url=env_value("LLM_BASE_URL") or None,
        bootstrap_admin_users=bootstrap_admin_users,
        runner_backend=runner_backend,
        runner_gateway_url=runner_gateway_url,
        runner_gateway_timeout_seconds=parse_timeout_seconds(
            env_value("RUNNER_GATEWAY_TIMEOUT_SECONDS")
        ),
        auth_mode=auth_mode,
        oa_auth_me_url=oa_auth_me_url,
        oa_auth_timeout_seconds=parse_positive_seconds(
            env_value("OA_AUTH_TIMEOUT_SECONDS"), 3.0, "OA_AUTH_TIMEOUT_SECONDS"
        ),
        oa_login_url=env_value("OA_LOGIN_URL") or None,
    )
