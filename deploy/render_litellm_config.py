from __future__ import annotations

import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

GENERAL_BUDGET_MARKER = "  # __AEGORA_BUDGET_GENERAL_SETTINGS__"
LITELLM_BUDGET_MARKER = "  # __AEGORA_BUDGET_LITELLM_SETTINGS__"
PRICING_MARKERS = {
    "PRIMARY_CHAT": "    # __AEGORA_PRIMARY_CHAT_MODEL_INFO__",
    "PRIMARY_FAST": "    # __AEGORA_PRIMARY_FAST_MODEL_INFO__",
    "FALLBACK_CHAT": "    # __AEGORA_FALLBACK_CHAT_MODEL_INFO__",
    "FALLBACK_FAST": "    # __AEGORA_FALLBACK_FAST_MODEL_INFO__",
}
REQUIRED_PROVIDER_ENV = (
    "LITELLM_MASTER_KEY",
    "LITELLM_UPSTREAM_API_KEY",
    "LITELLM_UPSTREAM_BASE_URL",
    "LITELLM_UPSTREAM_CHAT_MODEL",
    "LITELLM_UPSTREAM_FAST_MODEL",
    "LITELLM_FALLBACK_API_KEY",
    "LITELLM_FALLBACK_BASE_URL",
    "LITELLM_FALLBACK_CHAT_MODEL",
    "LITELLM_FALLBACK_FAST_MODEL",
)
SUPPORTED_GLOBAL_BUDGET_DURATION = "30d"


def _decimal_env(name: str) -> Decimal | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a decimal number") from error
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


def _yaml_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _render_pricing(prefix: str) -> str:
    input_cost = _decimal_env(f"LITELLM_{prefix}_INPUT_COST_PER_TOKEN")
    output_cost = _decimal_env(f"LITELLM_{prefix}_OUTPUT_COST_PER_TOKEN")
    if (input_cost is None) != (output_cost is None):
        raise ValueError(
            f"LITELLM_{prefix}_INPUT_COST_PER_TOKEN and "
            f"LITELLM_{prefix}_OUTPUT_COST_PER_TOKEN must be set together"
        )
    if input_cost is None:
        return "    # pricing resolved from LiteLLM's built-in model cost map"
    return (
        "    model_info:\n"
        f"      input_cost_per_token: {_yaml_decimal(input_cost)}\n"
        f"      output_cost_per_token: {_yaml_decimal(output_cost)}"
    )


def render(template: str) -> str:
    missing = [name for name in REQUIRED_PROVIDER_ENV if not os.getenv(name, "").strip()]
    if missing:
        raise ValueError("missing required LiteLLM environment variables: " + ", ".join(missing))

    budget = _decimal_env("LITELLM_MAX_BUDGET_USD")
    duration = os.getenv("LITELLM_BUDGET_DURATION", "30d").strip() or "30d"
    if budget is None:
        general_budget = "  # global dollar budget disabled"
        litellm_budget = "  # set LITELLM_MAX_BUDGET_USD to enable a hard global budget"
    else:
        if not os.getenv("LITELLM_DATABASE_URL", "").strip():
            raise ValueError("LITELLM_DATABASE_URL is required when LITELLM_MAX_BUDGET_USD is set")
        if duration != SUPPORTED_GLOBAL_BUDGET_DURATION:
            raise ValueError(
                "LITELLM_BUDGET_DURATION must currently be 30d for Aegora's global hard budget; "
                "other durations are blocked until the pinned LiteLLM release has verified reset semantics"
            )
        general_budget = (
            "  database_url: os.environ/LITELLM_DATABASE_URL\n"
            "  fail_closed_budget_enforcement: true"
        )
        litellm_budget = (
            f"  max_budget: {_yaml_decimal(budget)}\n"
            f"  budget_duration: \"{duration}\""
        )

    rendered = template.replace(GENERAL_BUDGET_MARKER, general_budget)
    rendered = rendered.replace(LITELLM_BUDGET_MARKER, litellm_budget)
    for prefix, marker in PRICING_MARKERS.items():
        rendered = rendered.replace(marker, _render_pricing(prefix))

    unresolved = [
        marker
        for marker in (GENERAL_BUDGET_MARKER, LITELLM_BUDGET_MARKER, *PRICING_MARKERS.values())
        if marker in rendered
    ]
    if unresolved:
        raise ValueError(f"unresolved LiteLLM template markers: {unresolved}")
    return rendered


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: render_litellm_config.py <template> <output>", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    try:
        target.write_text(render(source.read_text(encoding="utf-8")), encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"LiteLLM config render failed: {error}", file=sys.stderr)
        return 1
    print(f"Rendered LiteLLM config: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
