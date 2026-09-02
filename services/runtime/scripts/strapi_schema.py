#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from aegora_runtime.config import load_settings
from aegora_runtime.strapi import StrapiClient, StrapiError


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "integrations" / "strapi"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check or create Agent Strapi content types.")
    parser.add_argument("command", choices=["check", "create", "export"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    schemas = load_schemas()
    if args.command == "export":
        output = args.output or ROOT / "integrations" / "strapi" / "all-schemas.json"
        output.write_text(json.dumps(schemas, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote={output}")
        return

    client = StrapiClient(load_settings(validate_secrets=True).strapi)
    failed = 0
    for name, schema in schemas.items():
        endpoint = f"/api/{schema['info']['pluralName']}"
        exists = endpoint_exists(client, endpoint)
        if args.command == "check":
            if exists:
                print(json.dumps({"name": name, "endpoint": endpoint, "exists": True}, ensure_ascii=False))
            else:
                failed += 1
                print(json.dumps({"name": name, "endpoint": endpoint, "exists": False}, ensure_ascii=False))
        else:
            if exists:
                print(json.dumps({"name": name, "created": False, "skipped": "already_exists"}, ensure_ascii=False))
                continue
            try:
                client.request("POST", "/content-type-builder/content-types", content_type_payload(schema))
                print(json.dumps({"name": name, "created": True}, ensure_ascii=False))
            except StrapiError as exc:
                failed += 1
                print(json.dumps({"name": name, "created": False, "error": str(exc)}, ensure_ascii=False))
                print(f"manual_target=src/api/{schema['info']['singularName']}/content-types/{schema['info']['singularName']}/schema.json")
    if failed:
        raise SystemExit(1)


def endpoint_exists(client: StrapiClient, endpoint: str) -> bool:
    try:
        client.request("GET", f"{endpoint}?pagination[pageSize]=1")
        return True
    except StrapiError:
        return False


def load_schemas() -> dict[str, dict[str, Any]]:
    return {
        path.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_ROOT.glob("*/schema.json"))
    }


def content_type_payload(schema: dict[str, Any]) -> dict[str, Any]:
    info = schema["info"]
    return {
        "contentType": {
            "kind": schema["kind"],
            "collectionName": schema["collectionName"],
            "singularName": info["singularName"],
            "pluralName": info["pluralName"],
            "displayName": info["displayName"],
            "description": info.get("description", ""),
            "draftAndPublish": bool(schema.get("options", {}).get("draftAndPublish")),
            "pluginOptions": schema.get("pluginOptions") or {},
            "attributes": schema["attributes"],
        }
    }


if __name__ == "__main__":
    main()
