#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.skills import skill_content_hash, validate_skill
from aegora_runtime.strapi import StrapiClient, StrapiError, collection_endpoint


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / "integrations" / "strapi" / "skill" / "schema.json"
DEFAULT_INPUT = ROOT / "data" / "skills" / "aicoin_legacy_experiences.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and import Skill content into Strapi.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="Check whether configured Skill endpoint exists.")
    sub.add_parser("create-type", help="Try to create Skill Content-Type through Strapi Content-Type Builder API.")

    export_schema = sub.add_parser("export-schema", help="Print the Skill Content-Type schema JSON.")
    export_schema.add_argument("--output", type=Path)

    import_cmd = sub.add_parser("import", help="Upsert Skill JSON into configured Strapi Skill endpoint.")
    import_cmd.add_argument("--input", type=Path, default=DEFAULT_INPUT)

    args = parser.parse_args()
    settings = load_settings(validate_secrets=True)
    client = StrapiClient(settings.strapi)
    endpoint = collection_endpoint(settings, "skills_content")

    if args.command == "check":
        check(client, endpoint)
    elif args.command == "create-type":
        create_content_type(client)
    elif args.command == "export-schema":
        schema = load_schema()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote={args.output}")
        else:
            print(json.dumps(schema, ensure_ascii=False, indent=2))
    elif args.command == "import":
        import_skills(client, endpoint, args.input, settings.skills.max_content_chars)


def load_schema() -> dict[str, Any]:
    return json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))


def check(client: StrapiClient, endpoint: str) -> None:
    try:
        data = client.request("GET", f"{endpoint}?pagination[pageSize]=1")
        count = len(data.get("data") or [])
        print(json.dumps({"api_base": client.base_url, "endpoint": endpoint, "exists": True, "sample_count": count}, ensure_ascii=False))
    except StrapiError as exc:
        print(json.dumps({"api_base": client.base_url, "exists": False, "error": str(exc)}, ensure_ascii=False))


def create_content_type(client: StrapiClient) -> None:
    schema = load_schema()
    payload = {
        "contentType": {
            "kind": schema["kind"],
            "collectionName": schema["collectionName"],
            "singularName": schema["info"]["singularName"],
            "pluralName": schema["info"]["pluralName"],
            "displayName": schema["info"]["displayName"],
            "description": schema["info"].get("description", ""),
            "draftAndPublish": bool(schema.get("options", {}).get("draftAndPublish")),
            "pluginOptions": schema.get("pluginOptions") or {},
            "attributes": schema["attributes"],
        }
    }
    try:
        result = client.request("POST", "/content-type-builder/content-types", payload)
    except StrapiError as exc:
        print("content_type_create_failed")
        print(str(exc))
        print_manual_schema_instruction()
        raise SystemExit(1) from exc
    print(json.dumps({"created": True, "result": summarize_response(result)}, ensure_ascii=False, indent=2))
    print("如果 Strapi 提示需要重启，请重启本地 Strapi 后再执行 import。")


def import_skills(client: StrapiClient, endpoint: str, path: Path, max_content_chars: int) -> None:
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        rows = [rows]
    created = updated = failed = 0
    for row in rows:
        errors = validate_skill(row, max_content_chars)
        if errors:
            failed += 1
            print(json.dumps({"name": row.get("name"), "status": "invalid", "errors": errors}, ensure_ascii=False))
            continue
        payload = {"data": strapi_skill_payload(row)}
        existing = find_skill(client, endpoint, str(row["name"]))
        try:
            if existing:
                client.request("PUT", f"{endpoint}/{existing}", payload)
                updated += 1
                action = "updated"
            else:
                client.request("POST", endpoint, payload)
                created += 1
                action = "created"
            print(json.dumps({"name": row["name"], "status": action}, ensure_ascii=False))
        except StrapiError as exc:
            failed += 1
            print(json.dumps({"name": row["name"], "status": "failed", "error": str(exc)}, ensure_ascii=False))
    print(json.dumps({"input": str(path), "created": created, "updated": updated, "failed": failed}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


def find_skill(client: StrapiClient, endpoint: str, name: str) -> str | int | None:
    query = urllib.parse.urlencode({"filters[name][$eq]": name, "pagination[pageSize]": "1"})
    data = client.request("GET", f"{endpoint}?{query}")
    items = data.get("data") or []
    if not items:
        return None
    item = items[0]
    return item.get("documentId") or item.get("id")


def strapi_skill_payload(skill: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": skill["name"],
        "title": skill["title"],
        "description": skill["description"],
        "content": skill["content"],
        "product_id": skill.get("product_id"),
        "domain": skill.get("domain"),
        "skill_type": skill.get("skill_type", "guidance"),
        "source": skill.get("source", "manual"),
        "lifecycle_status": skill.get("status", "active"),
        "priority": int(skill.get("priority", 0)),
        "trigger_rules": skill.get("trigger_rules") or {},
        "metadata": skill.get("metadata") or {},
        "version": int(skill.get("version", 1)),
        "content_hash": skill_content_hash(skill),
        "reviewed_by": skill.get("reviewed_by"),
        "reviewed_at": skill.get("reviewed_at"),
    }


def summarize_response(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    if isinstance(data, dict):
        return {key: data.get(key) for key in ("uid", "apiID", "schema") if key in data}
    return {"keys": sorted(result.keys())}


def print_manual_schema_instruction() -> None:
    target = "src/api/skill/content-types/skill/schema.json"
    print(f"请把 {DEFAULT_SCHEMA} 复制到 Strapi 项目的 {target}，然后重启 Strapi。")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        raise
