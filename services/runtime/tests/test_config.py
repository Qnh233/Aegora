from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aegora_runtime.collections import collection_registry
from aegora_runtime.config import ConfigError, load_settings


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "settings.toml"


class ConfigTest(unittest.TestCase):
    def test_loads_defaults_without_secrets(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(CONFIG_PATH, env_path=None)

        self.assertEqual(settings.app.name, "aegora-runtime")
        self.assertEqual(settings.deepseek.base_url, "https://gateway.llmgtw.io/v1")
        self.assertEqual(settings.deepseek.chat_model, "deepseek-v4-pro")
        self.assertEqual(settings.deepseek.fast_model, "deepseek-v4-flash")
        self.assertFalse(settings.deepseek.enable_thinking)
        self.assertIsNone(settings.wecom_aibot.bot_id)
        self.assertIsNone(settings.wecom_aibot.secret)
        self.assertEqual(settings.wecom_aibot.ws_url, "")
        self.assertEqual(settings.wecom_aibot.max_reconnect_attempts, -1)
        self.assertTrue(settings.embedding.enable_startup_warmup)
        self.assertEqual(settings.embedding.provider, "siliconflow")
        self.assertIsNone(settings.embedding.api_key)
        self.assertEqual(settings.embedding.dimension, 1024)
        self.assertEqual(settings.embedding.storage_model, "siliconflow:BAAI/bge-m3")
        self.assertEqual(settings.postgres.port, 5432)
        self.assertEqual(settings.retrieval.top_k, 5)
        self.assertEqual(settings.retrieval.fusion_method, "rrf")
        self.assertEqual(settings.retrieval.rrf_k, 60)
        self.assertEqual(settings.retrieval.rrf_tie_break_source, "vector")
        self.assertEqual(settings.retrieval.candidate_k, 5)
        self.assertTrue(settings.skills.enabled)
        self.assertEqual(settings.skills.max_injected, 1)
        self.assertEqual(settings.skills.max_content_chars, 1000)
        self.assertEqual(settings.database.embedding_dim, 1024)
        self.assertEqual(
            settings.collections["faq_content"].strapi_endpoint,
            "/api/im-customer-service-knowledge-bases",
        )
        self.assertEqual(settings.collections["skills_content"].owner, "strapi")
        self.assertEqual(settings.collections["skills_content"].strapi_endpoint, "/api/im-customer-service-skills")
        self.assertEqual(settings.collections["skills_content"].pg_table, "skills")
        self.assertTrue(settings.collections["skills_content"].needs_embedding)
        self.assertEqual(settings.collections["chat_messages"].owner, "strapi")
        self.assertTrue(settings.collections["chat_messages"].runtime)
        self.assertEqual(settings.eval.retrieval_holdout_path.name, "eval_retrieval_holdout_300.jsonl")
        self.assertEqual(settings.agent.loop_mode, "planner")
        self.assertFalse(settings.agent.enable_llm_self_check)
        self.assertEqual(settings.agent.max_parallel_tool_calls, 4)
        self.assertEqual(settings.observability.pocoflow_db_path.name, "pocoflow.db")
        self.assertTrue(settings.observability.pocoflow_db_enabled)
        self.assertTrue(settings.observability.file_log_enabled)
        self.assertEqual(settings.observability.instance_id, "local")
        self.assertIsNone(settings.deepseek.api_key)

    def test_env_overrides_defaults(self) -> None:
        env = {
            "LLM_GATEWAY_API_KEY": "sk-test",
            "LLM_GATEWAY_BASE_URL": "https://llmgtw.local/v1/",
            "LLM_GATEWAY_ENABLE_THINKING": "true",
            "EMBEDDING_ENABLE_STARTUP_WARMUP": "false",
            "EMBEDDING_PROVIDER": "siliconflow",
            "EMBEDDING_API_KEY": "sf-test",
            "WECOM_AIBOT_ID": "bot-id",
            "WECOM_AIBOT_SECRET": "bot-secret",
            "WECOM_AIBOT_WS_URL": "wss://example.test/ws",
            "WECOM_AIBOT_SCENE": "2",
            "WECOM_AIBOT_PLUG_VERSION": "1.2.3",
            "WECOM_AIBOT_MAX_RECONNECT_ATTEMPTS": "3",
            "PG_HOST": "db.internal",
            "PG_PORT": "6543",
            "PG_PASSWORD": "secret",
            "AGENT_LOOP_MODE": "planner",
            "AGENT_ENABLE_LLM_SELF_CHECK": "true",
            "AGENT_MAX_PARALLEL_TOOL_CALLS": "2",
            "POCOFLOW_DB_ENABLED": "false",
            "FILE_LOG_ENABLED": "false",
            "INSTANCE_ID": "pod-2",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings(CONFIG_PATH, env_path=None)

        self.assertEqual(settings.deepseek.api_key, "sk-test")
        self.assertEqual(settings.deepseek.base_url, "https://llmgtw.local/v1")
        self.assertTrue(settings.deepseek.enable_thinking)
        self.assertFalse(settings.embedding.enable_startup_warmup)
        self.assertEqual(settings.wecom_aibot.bot_id, "bot-id")
        self.assertEqual(settings.wecom_aibot.secret, "bot-secret")
        self.assertEqual(settings.wecom_aibot.ws_url, "wss://example.test/ws")
        self.assertEqual(settings.wecom_aibot.scene, 2)
        self.assertEqual(settings.wecom_aibot.plug_version, "1.2.3")
        self.assertEqual(settings.wecom_aibot.max_reconnect_attempts, 3)
        self.assertEqual(settings.embedding.provider, "siliconflow")
        self.assertEqual(settings.embedding.api_key, "sf-test")
        self.assertEqual(settings.embedding.storage_model, "siliconflow:BAAI/bge-m3")
        self.assertEqual(settings.postgres.host, "db.internal")
        self.assertEqual(settings.postgres.port, 6543)
        self.assertIn("secret", settings.postgres.dsn)
        self.assertEqual(settings.agent.loop_mode, "planner")
        self.assertTrue(settings.agent.enable_llm_self_check)
        self.assertEqual(settings.agent.max_parallel_tool_calls, 2)
        self.assertFalse(settings.observability.pocoflow_db_enabled)
        self.assertFalse(settings.observability.file_log_enabled)
        self.assertEqual(settings.observability.instance_id, "pod-2")

    def test_legacy_deepseek_env_names_still_work(self) -> None:
        env = {
            "DEEPSEEK_API_KEY": "sk-legacy",
            "DEEPSEEK_BASE_URL": "https://deepseek.local/",
            "DEEPSEEK_CHAT_MODEL": "deepseek-chat",
            "DEEPSEEK_FAST_MODEL": "deepseek-fast",
            "DEEPSEEK_ENABLE_THINKING": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = load_settings(CONFIG_PATH, env_path=None)

        self.assertEqual(settings.deepseek.api_key, "sk-legacy")
        self.assertEqual(settings.deepseek.base_url, "https://deepseek.local")
        self.assertEqual(settings.deepseek.chat_model, "deepseek-chat")
        self.assertEqual(settings.deepseek.fast_model, "deepseek-fast")
        self.assertTrue(settings.deepseek.enable_thinking)

    def test_collection_registry_groups_sources(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(CONFIG_PATH, env_path=None)

        registry = collection_registry(settings)

        self.assertEqual(
            [item.name for item in registry.strapi()],
            ["faq_content", "skills_content", "chat_sessions", "chat_messages", "message_feedback"],
        )
        self.assertNotIn("skills_content", [item.name for item in registry.postgres()])
        self.assertNotIn("chat_messages", [item.name for item in registry.postgres()])
        self.assertIn("message_feedback", [item.name for item in registry.runtime()])
        self.assertIn("faq_content", [item.name for item in registry.needs_embedding()])

    def test_rejects_invalid_collection_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "settings.toml"
            config_path.write_text(
                CONFIG_PATH.read_text(encoding="utf-8")
                + "\n[collections.bad]\nowner = \"unknown\"\npg_table = \"bad\"\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(ConfigError):
                    load_settings(config_path, env_path=None)

    def test_dotenv_does_not_override_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("PG_HOST=from-file\nPG_PORT=1111\n", encoding="utf-8")
            with patch.dict(os.environ, {"PG_HOST": "from-process"}, clear=True):
                settings = load_settings(CONFIG_PATH, env_path=env_path)

        self.assertEqual(settings.postgres.host, "from-process")
        self.assertEqual(settings.postgres.port, 1111)

    def test_validate_runtime_secrets(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ConfigError):
                load_settings(CONFIG_PATH, env_path=None, validate_secrets=True)


if __name__ == "__main__":
    unittest.main()
