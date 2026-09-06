from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

DEPLOY_DIR = Path(__file__).resolve().parent
if str(DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(DEPLOY_DIR))

from render_litellm_config import render

TEMPLATE = Path(__file__).with_name("litellm-config.yaml").read_text(encoding="utf-8")
BASE_ENV = {
    "LITELLM_MASTER_KEY": "sk-test",
    "LITELLM_UPSTREAM_API_KEY": "primary-key",
    "LITELLM_UPSTREAM_BASE_URL": "https://primary.example/v1",
    "LITELLM_UPSTREAM_CHAT_MODEL": "openai/chat-primary",
    "LITELLM_UPSTREAM_FAST_MODEL": "openai/fast-primary",
    "LITELLM_FALLBACK_API_KEY": "fallback-key",
    "LITELLM_FALLBACK_BASE_URL": "https://fallback.example/v1",
    "LITELLM_FALLBACK_CHAT_MODEL": "openai/chat-fallback",
    "LITELLM_FALLBACK_FAST_MODEL": "openai/fast-fallback",
}


class RenderLiteLLMConfigTests(unittest.TestCase):
    def render_with(self, **extra: str) -> str:
        with patch.dict(os.environ, {**BASE_ENV, **extra}, clear=True):
            return render(TEMPLATE)

    def test_default_render_keeps_fallbacks_and_disables_budget(self) -> None:
        rendered = self.render_with()
        self.assertIn('aegora-chat: ["aegora-chat-fallback"]', rendered)
        self.assertIn('aegora-fast: ["aegora-fast-fallback"]', rendered)
        self.assertIn("# global dollar budget disabled", rendered)
        self.assertNotIn("database_url:", rendered)
        self.assertNotIn("max_budget:", rendered)
        self.assertNotIn("__AEGORA_", rendered)

    def test_budget_requires_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "LITELLM_DATABASE_URL"):
            self.render_with(LITELLM_MAX_BUDGET_USD="25")

    def test_budget_renders_numeric_value_fail_closed_and_pricing(self) -> None:
        rendered = self.render_with(
            LITELLM_MAX_BUDGET_USD="25.50",
            LITELLM_BUDGET_DURATION="30d",
            LITELLM_DATABASE_URL="postgresql://litellm:secret@db/litellm",
            LITELLM_PRIMARY_CHAT_INPUT_COST_PER_TOKEN="0.000001",
            LITELLM_PRIMARY_CHAT_OUTPUT_COST_PER_TOKEN="0.000002",
        )
        self.assertIn("database_url: os.environ/LITELLM_DATABASE_URL", rendered)
        self.assertIn("fail_closed_budget_enforcement: true", rendered)
        self.assertIn("max_budget: 25.5", rendered)
        self.assertIn('budget_duration: "30d"', rendered)
        self.assertIn("input_cost_per_token: 0.000001", rendered)
        self.assertIn("output_cost_per_token: 0.000002", rendered)

    def test_budget_rejects_unverified_non_30d_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "must currently be 30d"):
            self.render_with(
                LITELLM_MAX_BUDGET_USD="25",
                LITELLM_BUDGET_DURATION="1d",
                LITELLM_DATABASE_URL="postgresql://litellm:secret@db/litellm",
            )

    def test_pricing_pair_must_be_complete(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be set together"):
            self.render_with(LITELLM_FALLBACK_FAST_INPUT_COST_PER_TOKEN="0.000001")

    def test_fallback_provider_is_required(self) -> None:
        env = dict(BASE_ENV)
        env.pop("LITELLM_FALLBACK_API_KEY")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "LITELLM_FALLBACK_API_KEY"):
                render(TEMPLATE)


if __name__ == "__main__":
    unittest.main()
