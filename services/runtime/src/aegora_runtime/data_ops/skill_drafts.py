from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from aegora_runtime.config import Settings
from aegora_runtime.skills import skill_content_hash, validate_skill
from aegora_runtime.strapi import StrapiClient, collection_endpoint


def write_skill_draft(settings: Settings, draft: dict[str, Any]) -> dict[str, Any]:
    errors = validate_skill({**draft, "status": "draft"}, settings.skills.max_content_chars)
    if errors:
        raise ValueError("; ".join(errors))
    client = StrapiClient(settings.strapi)
    endpoint = collection_endpoint(settings, "skills_content")
    name = draft["name"]
    payload = {
        "name": name,
        "title": draft["title"],
        "description": draft["description"],
        "content": draft["content"],
        "product_id": draft.get("product_id") or settings.app.product_id,
        "domain": draft.get("domain"),
        "skill_type": draft.get("skill_type", "guidance"),
        "source": "agent",
        "lifecycle_status": "draft",
        "priority": int(draft.get("priority", 0)),
        "trigger_rules": draft.get("trigger_rules") or {},
        "metadata": draft.get("metadata") or {},
        "version": 1,
        "content_hash": skill_content_hash({**draft, "source": "agent", "status": "draft"}),
    }
    return client.upsert(endpoint, "name", name, payload)


def draft_name(title: str, *, now: datetime, salt: str) -> str:
    digest = hashlib.sha1(f"{title}\n{salt}".encode("utf-8")).hexdigest()[:10]
    return f"reflection_{now:%Y%m%d}_{digest}"
