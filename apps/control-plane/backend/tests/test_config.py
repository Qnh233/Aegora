from app.config import (
    DEFAULT_CORS_ORIGINS,
    get_config,
    get_cors_origins,
    normalize_auth_mode,
    normalize_runner_backend,
    parse_env_file,
)
from app.db import normalize_agent_model


def test_parse_env_file_reads_plain_key_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
        # comment
        DATABASE_URL=postgresql://db
        LLM_API_KEY='key'
        LLM_MODEL="model"
        LLM_BASE_URL=
        BOOTSTRAP_ADMIN_USERS=u_console, u_ops
        CORS_ORIGINS=http://127.0.0.1:5173, http://192.168.152.69:5173
        RUNNER_BACKEND=gateway
        RUNNER_GATEWAY_URL=http://runner:5000
        RUNNER_GATEWAY_TIMEOUT_SECONDS=12.5
        AUTH_MODE=oa
        OA_AUTH_ME_URL=http://oa:8000/api/oa/me
        OA_AUTH_TIMEOUT_SECONDS=2.5
        OA_LOGIN_URL=http://oa-login
        """,
        encoding="utf-8",
    )

    values = parse_env_file(env_file)

    assert values["DATABASE_URL"] == "postgresql://db"
    assert values["LLM_API_KEY"] == "key"
    assert values["LLM_MODEL"] == "model"
    assert values["LLM_BASE_URL"] == ""
    assert values["BOOTSTRAP_ADMIN_USERS"] == "u_console, u_ops"
    assert values["CORS_ORIGINS"] == "http://127.0.0.1:5173, http://192.168.152.69:5173"
    assert values["RUNNER_BACKEND"] == "gateway"
    assert values["RUNNER_GATEWAY_URL"] == "http://runner:5000"
    assert values["AUTH_MODE"] == "oa"
    assert values["OA_AUTH_ME_URL"] == "http://oa:8000/api/oa/me"


def test_get_config_prefers_real_environment_over_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://file\nLLM_API_KEY=file-key\nLLM_MODEL=file-model\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_URL", "postgresql://env")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    get_config.cache_clear()

    config = get_config()

    assert config.database_url == "postgresql://env"
    assert config.llm_api_key == "file-key"
    assert config.llm_model == "file-model"
    assert config.bootstrap_admin_users == ()
    assert config.runner_backend == "debug"
    get_config.cache_clear()


def test_get_config_reads_gateway_runner_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://db",
                "RUNNER_BACKEND=gateway",
                "RUNNER_GATEWAY_URL=http://192.168.152.34:5000",
                "RUNNER_GATEWAY_TIMEOUT_SECONDS=9",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RUNNER_BACKEND", raising=False)
    monkeypatch.delenv("RUNNER_GATEWAY_URL", raising=False)
    monkeypatch.delenv("RUNNER_GATEWAY_TIMEOUT_SECONDS", raising=False)
    get_config.cache_clear()

    config = get_config()

    assert config.runner_backend == "gateway"
    assert config.runner_gateway_url == "http://192.168.152.34:5000"
    assert config.runner_gateway_timeout_seconds == 9
    get_config.cache_clear()


def test_get_config_reads_oa_auth_settings(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql://db",
                "AUTH_MODE=oa",
                "OA_AUTH_ME_URL=http://oa:8000/api/oa/me",
                "OA_AUTH_TIMEOUT_SECONDS=2",
                "OA_LOGIN_URL=http://oa-login",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("OA_AUTH_ME_URL", raising=False)
    monkeypatch.delenv("OA_AUTH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("OA_LOGIN_URL", raising=False)
    get_config.cache_clear()

    config = get_config()

    assert config.auth_mode == "oa"
    assert config.oa_auth_me_url == "http://oa:8000/api/oa/me"
    assert config.oa_auth_timeout_seconds == 2
    assert config.oa_login_url == "http://oa-login"
    get_config.cache_clear()


def test_get_cors_origins_keeps_local_default(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://db\n", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    assert get_cors_origins() == DEFAULT_CORS_ORIGINS


def test_get_cors_origins_reads_configured_origins(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CORS_ORIGINS=http://127.0.0.1:5173, http://192.168.152.69:5173\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    assert get_cors_origins() == (
        "http://127.0.0.1:5173",
        "http://192.168.152.69:5173",
    )


def test_normalize_agent_model_keeps_empty_model_as_global_fallback():
    assert normalize_agent_model(None) is None
    assert normalize_agent_model("") is None
    assert normalize_agent_model("   ") is None
    assert normalize_agent_model(" mimo-v2.5-pro ") == "mimo-v2.5-pro"


def test_normalize_runner_backend_rejects_unknown_backend():
    assert normalize_runner_backend("") == "debug"
    assert normalize_runner_backend("GATEWAY") == "gateway"
    try:
        normalize_runner_backend("cloud")
    except RuntimeError as error:
        assert "debug 或 gateway" in str(error)
    else:
        raise AssertionError("unknown backend should fail")


def test_normalize_auth_mode_rejects_unknown_mode():
    assert normalize_auth_mode("") == "local"
    assert normalize_auth_mode("OA") == "oa"
    try:
        normalize_auth_mode("session")
    except RuntimeError as error:
        assert "local 或 oa" in str(error)
    else:
        raise AssertionError("unknown auth mode should fail")
