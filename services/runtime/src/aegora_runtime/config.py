from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus


SERVICE_ROOT = Path(__file__).resolve().parents[2]


def find_aegora_root(start: Path = SERVICE_ROOT) -> Path:
    if configured := os.getenv("AEGORA_ROOT"):
        return Path(configured).expanduser().resolve()
    for parent in (start, *start.parents):
        if (parent / "AGENTS.md").exists() and (parent / "apps").exists() and (parent / "services").exists():
            return parent
    return SERVICE_ROOT


AEGORA_ROOT = find_aegora_root()
DEFAULT_CONFIG_PATH = SERVICE_ROOT / "config" / "settings.toml"
DEFAULT_ENV_PATH = AEGORA_ROOT / ".env"


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class AppSettings:
    name: str
    env: str
    log_level: str
    product_id: str


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str | None
    base_url: str
    chat_model: str
    fast_model: str
    timeout_seconds: int
    enable_thinking: bool = False
    trace_metadata_enabled: bool = True


@dataclass(frozen=True)
class StrapiSettings:
    base_url: str | None
    api_token: str | None
    timeout_seconds: int


@dataclass(frozen=True)
class WeComAIBotSettings:
    bot_id: str | None
    secret: str | None
    ws_url: str
    scene: int | None
    plug_version: str | None
    reconnect_interval_ms: int
    max_reconnect_attempts: int
    heartbeat_interval_ms: int
    request_timeout_ms: int


@dataclass(frozen=True)
class PostgresSettings:
    host: str
    port: int
    database: str
    user: str
    password: str | None
    sslmode: str
    pool_min_size: int
    pool_max_size: int

    @property
    def dsn(self) -> str:
        password = f":{quote_plus(self.password)}" if self.password else ""
        user = quote_plus(self.user)
        database = quote_plus(self.database)
        return f"postgresql://{user}{password}@{self.host}:{self.port}/{database}?sslmode={self.sslmode}"


@dataclass(frozen=True)
class EmbeddingSettings:
    provider: str
    api_key: str | None
    base_url: str
    model: str
    local_path: Path
    dimension: int
    timeout_seconds: int
    batch_size: int
    max_retries: int
    max_length: int
    enable_startup_warmup: bool

    @property
    def storage_model(self) -> str:
        return self.model if self.provider == "local" else f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class RetrievalSettings:
    fusion_method: str
    rrf_k: int
    rrf_tie_break_source: str
    top_k: int
    candidate_k: int
    min_score: float


@dataclass(frozen=True)
class SkillSettings:
    enabled: bool
    candidate_k: int
    max_injected: int
    max_content_chars: int
    min_score: float
    commercial_min_score: float
    manual_boost: float
    agent_penalty: float
    product_boost: float
    domain_boost: float


@dataclass(frozen=True)
class AgentSettings:
    loop_mode: str
    max_iterations: int
    timeout_seconds: int
    token_budget: int
    enable_llm_self_check: bool
    max_parallel_tool_calls: int


@dataclass(frozen=True)
class DatabaseSettings:
    schema: str
    embedding_dim: int
    embedding_model: str
    preferred_text_search_config: str
    fallback_text_search_config: str


@dataclass(frozen=True)
class CollectionSettings:
    name: str
    owner: str
    pg_table: str | None = None
    strapi_endpoint: str | None = None
    sync_direction: str = "none"
    needs_embedding: bool = False
    runtime: bool = False
    description: str = ""


@dataclass(frozen=True)
class EvalSettings:
    core_path: Path
    retrieval_path: Path
    retrieval_holdout_path: Path
    bad_feedback_path: Path
    security_path: Path
    perf_seed_path: Path


@dataclass(frozen=True)
class ObservabilitySettings:
    pocoflow_db_path: Path
    log_dir: Path
    pocoflow_db_enabled: bool
    file_log_enabled: bool
    instance_id: str
    langfuse_tracing_enabled: bool


@dataclass(frozen=True)
class Settings:
    app: AppSettings
    deepseek: DeepSeekSettings
    strapi: StrapiSettings
    wecom_aibot: WeComAIBotSettings
    postgres: PostgresSettings
    embedding: EmbeddingSettings
    retrieval: RetrievalSettings
    skills: SkillSettings
    database: DatabaseSettings
    collections: dict[str, CollectionSettings]
    agent: AgentSettings
    observability: ObservabilitySettings
    eval: EvalSettings

    def validate_runtime_secrets(self) -> None:
        missing = []
        if not self.deepseek.api_key:
            missing.append("LLM_GATEWAY_API_KEY")
        if not self.strapi.base_url:
            missing.append("STRAPI_BASE_URL")
        if not self.strapi.api_token:
            missing.append("STRAPI_API_TOKEN")
        if not self.postgres.password:
            missing.append("PG_PASSWORD")
        if self.embedding.provider == "siliconflow" and not self.embedding.api_key:
            missing.append("EMBEDDING_API_KEY")
        if missing:
            raise ConfigError("缺少运行时配置: " + ", ".join(missing))


def load_settings(
    config_path: Path | str = DEFAULT_CONFIG_PATH,
    env_path: Path | str | None = DEFAULT_ENV_PATH,
    *,
    validate_secrets: bool = False,
) -> Settings:
    """Load typed settings from TOML defaults plus environment variables.

    Environment variables always take precedence. A local `.env` file is loaded
    first when present, but it does not override variables that already exist in
    the process environment.
    """

    config_path = Path(config_path)
    if env_path is not None:
        load_env_file(Path(env_path))

    with config_path.open("rb") as f:
        raw = tomllib.load(f)

    settings = Settings(
        app=_load_app(raw["app"]),
        deepseek=_load_deepseek(raw["deepseek"]),
        strapi=_load_strapi(raw["strapi"]),
        wecom_aibot=_load_wecom_aibot(raw["wecom_aibot"]),
        postgres=_load_postgres(raw["postgres"]),
        embedding=_load_embedding(raw["embedding"]),
        retrieval=_load_retrieval(raw["retrieval"]),
        skills=_load_skills(raw["skills"]),
        database=_load_database(raw["database"]),
        collections=_load_collections(raw.get("collections", {})),
        agent=_load_agent(raw["agent"]),
        observability=_load_observability(raw["observability"]),
        eval=_load_eval(raw["eval"]),
    )
    _validate_numeric_ranges(settings)
    if validate_secrets:
        settings.validate_runtime_secrets()
    return settings


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_quotes(value.strip())
        os.environ.setdefault(key, value)


def _load_app(raw: dict[str, Any]) -> AppSettings:
    return AppSettings(
        name=str(raw["name"]),
        env=_env(raw["env_var"], "local"),
        log_level=_env(raw["log_level_env_var"], "INFO"),
        product_id=_env(raw["product_id_env_var"], raw["default_product_id"]),
    )


def _load_deepseek(raw: dict[str, Any]) -> DeepSeekSettings:
    enable_thinking = _env_optional_bool(raw["enable_thinking_env_var"])
    return DeepSeekSettings(
        api_key=(
            _env_optional(raw["api_key_env_var"])
            or _env_optional("DEEPSEEK_API_KEY")
            or _env_optional("SILICONFLOW_API_KEY")
        ),
        base_url=(
            _env_optional(raw["base_url_env_var"])
            or _env_optional("DEEPSEEK_BASE_URL")
            or raw["default_base_url"]
        ).rstrip("/"),
        chat_model=(
            _env_optional(raw["chat_model_env_var"])
            or _env_optional("DEEPSEEK_CHAT_MODEL")
            or raw["default_chat_model"]
        ),
        fast_model=(
            _env_optional(raw["fast_model_env_var"])
            or _env_optional("DEEPSEEK_FAST_MODEL")
            or raw["default_fast_model"]
        ),
        timeout_seconds=_env_optional_int(raw["timeout_seconds_env_var"])
        or _env_optional_int("DEEPSEEK_TIMEOUT_SECONDS")
        or int(raw["default_timeout_seconds"]),
        enable_thinking=enable_thinking
        if enable_thinking is not None
        else _env_bool("DEEPSEEK_ENABLE_THINKING", raw["default_enable_thinking"]),
        trace_metadata_enabled=_env_bool(
            raw["trace_metadata_enabled_env_var"],
            raw["default_trace_metadata_enabled"],
        ),
    )


def _load_strapi(raw: dict[str, Any]) -> StrapiSettings:
    base_url = _env_optional(raw["base_url_env_var"])
    return StrapiSettings(
        base_url=base_url.rstrip("/") if base_url else None,
        api_token=_env_optional(raw["api_token_env_var"]),
        timeout_seconds=_env_int(raw["timeout_seconds_env_var"], raw["default_timeout_seconds"]),
    )


def _load_wecom_aibot(raw: dict[str, Any]) -> WeComAIBotSettings:
    return WeComAIBotSettings(
        bot_id=_env_optional(raw["bot_id_env_var"]),
        secret=_env_optional(raw["secret_env_var"]),
        ws_url=_env(raw["ws_url_env_var"], raw["default_ws_url"]),
        scene=_env_optional_int(raw["scene_env_var"]),
        plug_version=_env_optional(raw["plug_version_env_var"]),
        reconnect_interval_ms=_env_int(
            raw["reconnect_interval_ms_env_var"],
            raw["default_reconnect_interval_ms"],
        ),
        max_reconnect_attempts=_env_int(
            raw["max_reconnect_attempts_env_var"],
            raw["default_max_reconnect_attempts"],
        ),
        heartbeat_interval_ms=_env_int(
            raw["heartbeat_interval_ms_env_var"],
            raw["default_heartbeat_interval_ms"],
        ),
        request_timeout_ms=_env_int(
            raw["request_timeout_ms_env_var"],
            raw["default_request_timeout_ms"],
        ),
    )


def _load_postgres(raw: dict[str, Any]) -> PostgresSettings:
    return PostgresSettings(
        host=_env(raw["host_env_var"], raw["default_host"]),
        port=_env_int(raw["port_env_var"], raw["default_port"]),
        database=_env(raw["database_env_var"], raw["default_database"]),
        user=_env(raw["user_env_var"], raw["default_user"]),
        password=_env_optional(raw["password_env_var"]),
        sslmode=_env(raw["sslmode_env_var"], raw["default_sslmode"]),
        pool_min_size=_env_int(raw["pool_min_size_env_var"], raw["default_pool_min_size"]),
        pool_max_size=_env_int(raw["pool_max_size_env_var"], raw["default_pool_max_size"]),
    )


def _load_embedding(raw: dict[str, Any]) -> EmbeddingSettings:
    return EmbeddingSettings(
        provider=_env(raw["provider_env_var"], raw["default_provider"]),
        api_key=_env_optional(raw["api_key_env_var"]),
        base_url=_env(raw["base_url_env_var"], raw["default_base_url"]).rstrip("/"),
        model=_env(raw["model_env_var"], raw["default_model"]),
        local_path=Path(_env(raw["local_path_env_var"], raw["default_local_path"])).expanduser(),
        dimension=_env_int(raw["dimension_env_var"], raw["default_dimension"]),
        timeout_seconds=_env_int(raw["timeout_seconds_env_var"], raw["default_timeout_seconds"]),
        batch_size=_env_int(raw["batch_size_env_var"], raw["default_batch_size"]),
        max_retries=_env_int(raw["max_retries_env_var"], raw["default_max_retries"]),
        max_length=_env_int(raw["max_length_env_var"], raw["default_max_length"]),
        enable_startup_warmup=_env_bool(raw["enable_startup_warmup_env_var"], raw["default_enable_startup_warmup"]),
    )


def _load_retrieval(raw: dict[str, Any]) -> RetrievalSettings:
    return RetrievalSettings(
        fusion_method=str(raw["fusion_method"]),
        rrf_k=int(raw["rrf_k"]),
        rrf_tie_break_source=str(raw["rrf_tie_break_source"]),
        top_k=int(raw["top_k"]),
        candidate_k=int(raw["candidate_k"]),
        min_score=float(raw["min_score"]),
    )


def _load_skills(raw: dict[str, Any]) -> SkillSettings:
    return SkillSettings(
        enabled=_env_bool(raw["enabled_env_var"], raw["default_enabled"]),
        candidate_k=int(raw["candidate_k"]),
        max_injected=int(raw["max_injected"]),
        max_content_chars=int(raw["max_content_chars"]),
        min_score=float(raw["min_score"]),
        commercial_min_score=float(raw["commercial_min_score"]),
        manual_boost=float(raw["manual_boost"]),
        agent_penalty=float(raw["agent_penalty"]),
        product_boost=float(raw["product_boost"]),
        domain_boost=float(raw["domain_boost"]),
    )


def _load_agent(raw: dict[str, Any]) -> AgentSettings:
    return AgentSettings(
        loop_mode=_env(raw["loop_mode_env_var"], raw["default_loop_mode"]),
        max_iterations=_env_int(raw["max_iterations_env_var"], raw["default_max_iterations"]),
        timeout_seconds=_env_int(raw["timeout_seconds_env_var"], raw["default_timeout_seconds"]),
        token_budget=_env_int(raw["token_budget_env_var"], raw["default_token_budget"]),
        enable_llm_self_check=_env_bool(raw["enable_llm_self_check_env_var"], raw["default_enable_llm_self_check"]),
        max_parallel_tool_calls=_env_int(
            raw["max_parallel_tool_calls_env_var"],
            raw["default_max_parallel_tool_calls"],
        ),
    )


def _load_database(raw: dict[str, Any]) -> DatabaseSettings:
    return DatabaseSettings(
        schema=str(raw["schema"]),
        embedding_dim=int(raw["embedding_dim"]),
        embedding_model=str(raw["embedding_model"]),
        preferred_text_search_config=str(raw["preferred_text_search_config"]),
        fallback_text_search_config=str(raw["fallback_text_search_config"]),
    )


def _load_collections(raw: dict[str, Any]) -> dict[str, CollectionSettings]:
    collections = {}
    for name, item in raw.items():
        if not isinstance(item, dict):
            raise ConfigError(f"collections.{name} 必须是表配置对象")
        collections[str(name)] = CollectionSettings(
            name=str(name),
            owner=str(item.get("owner", "")),
            pg_table=_optional_str(item.get("pg_table")),
            strapi_endpoint=_optional_str(item.get("strapi_endpoint")),
            sync_direction=str(item.get("sync_direction", "none")),
            needs_embedding=bool(item.get("needs_embedding", False)),
            runtime=bool(item.get("runtime", False)),
            description=str(item.get("description", "")),
        )
    return collections


def _load_eval(raw: dict[str, Any]) -> EvalSettings:
    return EvalSettings(
        core_path=_project_path(raw["core_path"]),
        retrieval_path=_project_path(raw["retrieval_path"]),
        retrieval_holdout_path=_project_path(raw["retrieval_holdout_path"]),
        bad_feedback_path=_project_path(raw["bad_feedback_path"]),
        security_path=_project_path(raw["security_path"]),
        perf_seed_path=_project_path(raw["perf_seed_path"]),
    )


def _load_observability(raw: dict[str, Any]) -> ObservabilitySettings:
    return ObservabilitySettings(
        pocoflow_db_path=_project_path(raw["pocoflow_db_path"]),
        log_dir=_project_path(raw["log_dir"]),
        pocoflow_db_enabled=_env_bool(
            raw["pocoflow_db_enabled_env_var"],
            raw["default_pocoflow_db_enabled"],
        ),
        file_log_enabled=_env_bool(
            raw["file_log_enabled_env_var"],
            raw["default_file_log_enabled"],
        ),
        instance_id=_env(raw["instance_id_env_var"], raw["default_instance_id"]),
        langfuse_tracing_enabled=_env_bool(
            raw["langfuse_tracing_enabled_env_var"],
            raw["default_langfuse_tracing_enabled"],
        ),
    )


def _validate_numeric_ranges(settings: Settings) -> None:
    if settings.retrieval.fusion_method != "rrf":
        raise ConfigError("retrieval.fusion_method 当前仅支持 rrf")
    if settings.retrieval.rrf_k <= 0:
        raise ConfigError("retrieval.rrf_k 必须大于 0")
    if settings.retrieval.rrf_tie_break_source not in {"fulltext", "vector"}:
        raise ConfigError("retrieval.rrf_tie_break_source 必须是 fulltext 或 vector")
    if settings.retrieval.top_k <= 0:
        raise ConfigError("retrieval.top_k 必须大于 0")
    if settings.retrieval.candidate_k < settings.retrieval.top_k:
        raise ConfigError("retrieval.candidate_k 必须大于等于 top_k")
    if settings.skills.candidate_k <= 0 or settings.skills.max_injected <= 0:
        raise ConfigError("skills.candidate_k 和 skills.max_injected 必须大于 0")
    if settings.skills.max_content_chars <= 0:
        raise ConfigError("skills.max_content_chars 必须大于 0")
    if not 0 <= settings.skills.min_score <= settings.skills.commercial_min_score <= 1:
        raise ConfigError("skills 相似度阈值必须满足 0 <= min_score <= commercial_min_score <= 1")
    if settings.postgres.pool_min_size < 0:
        raise ConfigError("postgres.pool_min_size 不能小于 0")
    if settings.postgres.pool_max_size < settings.postgres.pool_min_size:
        raise ConfigError("postgres.pool_max_size 必须大于等于 pool_min_size")
    if settings.agent.max_iterations <= 0:
        raise ConfigError("agent.max_iterations 必须大于 0")
    if settings.agent.max_parallel_tool_calls <= 0:
        raise ConfigError("agent.max_parallel_tool_calls 必须大于 0")
    if settings.agent.loop_mode != "planner":
        raise ConfigError("agent.loop_mode 当前仅支持 planner")
    if settings.database.embedding_dim <= 0:
        raise ConfigError("database.embedding_dim 必须大于 0")
    if settings.embedding.batch_size <= 0:
        raise ConfigError("embedding.batch_size 必须大于 0")
    if settings.embedding.max_retries <= 0:
        raise ConfigError("embedding.max_retries 必须大于 0")
    if settings.embedding.provider not in {"local", "siliconflow"}:
        raise ConfigError("embedding.provider 必须是 local 或 siliconflow")
    if settings.embedding.provider == "local" and str(settings.embedding.local_path) in {"", "."}:
        raise ConfigError("embedding.local_path 在 local provider 下不能为空")
    if settings.embedding.dimension != settings.database.embedding_dim:
        raise ConfigError("embedding.dimension 必须与 database.embedding_dim 一致")
    if settings.wecom_aibot.reconnect_interval_ms <= 0:
        raise ConfigError("wecom_aibot.reconnect_interval_ms 必须大于 0")
    if settings.wecom_aibot.heartbeat_interval_ms <= 0:
        raise ConfigError("wecom_aibot.heartbeat_interval_ms 必须大于 0")
    if settings.wecom_aibot.request_timeout_ms <= 0:
        raise ConfigError("wecom_aibot.request_timeout_ms 必须大于 0")
    _validate_collections(settings.collections)


def _validate_collections(collections: dict[str, CollectionSettings]) -> None:
    for name, collection in collections.items():
        if collection.owner not in {"postgres", "strapi"}:
            raise ConfigError(f"collections.{name}.owner 必须是 postgres 或 strapi")
        if collection.owner == "postgres" and not collection.pg_table:
            raise ConfigError(f"collections.{name}.pg_table 在 postgres owner 下不能为空")
        if collection.owner == "strapi" and not collection.strapi_endpoint:
            raise ConfigError(f"collections.{name}.strapi_endpoint 在 strapi owner 下不能为空")
        if collection.strapi_endpoint and not collection.strapi_endpoint.startswith("/api/"):
            raise ConfigError(f"collections.{name}.strapi_endpoint 必须以 /api/ 开头")
        if collection.sync_direction not in {"none", "strapi_to_pg", "pg_to_strapi"}:
            raise ConfigError(f"collections.{name}.sync_direction 不支持: {collection.sync_direction}")
        if collection.needs_embedding and not collection.pg_table:
            raise ConfigError(f"collections.{name}.needs_embedding=true 时必须配置 pg_table")


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else SERVICE_ROOT / path


def _env(name: str, default: Any) -> str:
    value = os.environ.get(name)
    return str(default) if value is None or value == "" else value


def _env_optional(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _env_int(name: str, default: Any) -> int:
    value = _env(name, default)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值: {value}") from exc


def _env_optional_int(name: str) -> int | None:
    value = _env_optional(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前值: {value}") from exc


def _env_optional_bool(name: str) -> bool | None:
    value = _env_optional(name)
    if value is None:
        return None
    return _env_bool(name, value)


def _env_bool(name: str, default: Any) -> bool:
    value = _env(name, default).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是布尔值，当前值: {value}")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
