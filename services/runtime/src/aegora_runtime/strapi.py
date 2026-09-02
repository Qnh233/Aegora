from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aegora_runtime.config import Settings, StrapiSettings


class StrapiError(RuntimeError):
    pass


class StrapiClient:
    def __init__(self, settings: StrapiSettings):
        if not settings.base_url:
            raise StrapiError("STRAPI_BASE_URL is required")
        if not settings.api_token:
            raise StrapiError("STRAPI_API_TOKEN is required")
        base_url = settings.base_url.rstrip("/")
        self.base_url = base_url[:-6] if base_url.endswith("/admin") else base_url
        self.api_token = settings.api_token
        self.timeout_seconds = settings.timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise StrapiError(f"Strapi HTTP {exc.code} {method} {path}: {detail[:1000]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise StrapiError(f"Strapi request failed {method} {path}: {exc}") from exc
        if not body:
            return {}
        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise StrapiError(f"Strapi returned invalid JSON {method} {path}: {body[:500]}") from exc
        if not isinstance(result, dict):
            raise StrapiError(f"Strapi returned non-object JSON {method} {path}")
        return result

    def list(
        self,
        endpoint: str,
        *,
        filters: dict[str, Any] | None = None,
        sort: str | None = None,
        page_size: int = 100,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        page = 1
        result: list[dict[str, Any]] = []
        while True:
            params = flatten_query(filters or {})
            params["pagination[page]"] = str(page)
            params["pagination[pageSize]"] = str(page_size)
            if sort:
                params["sort"] = sort
            payload = self.request("GET", f"{endpoint}?{urllib.parse.urlencode(params)}")
            rows = payload.get("data") or []
            if not isinstance(rows, list):
                raise StrapiError(f"Strapi list response has invalid data: {endpoint}")
            result.extend(normalize_entity(row) for row in rows if isinstance(row, dict))
            if max_items is not None and len(result) >= max_items:
                return result[:max_items]
            page_count = ((payload.get("meta") or {}).get("pagination") or {}).get("pageCount")
            if not rows or page_count is None or page >= int(page_count):
                break
            page += 1
        return result

    def find_one(self, endpoint: str, field: str, value: Any) -> dict[str, Any] | None:
        rows = self.list(endpoint, filters={f"filters[{field}][$eq]": value}, page_size=1, max_items=1)
        return rows[0] if rows else None

    def create(self, endpoint: str, data: dict[str, Any]) -> dict[str, Any]:
        return normalize_entity(self.request("POST", endpoint, {"data": data}).get("data") or {})

    def update(self, endpoint: str, entity_id: str | int, data: dict[str, Any]) -> dict[str, Any]:
        payload = self.request("PUT", f"{endpoint}/{entity_id}", {"data": data})
        return normalize_entity(payload.get("data") or {})

    def upsert(self, endpoint: str, field: str, value: Any, data: dict[str, Any]) -> dict[str, Any]:
        existing = self.find_one(endpoint, field, value)
        if existing:
            return self.update(endpoint, entity_reference(existing), data)
        return self.create(endpoint, data)


def client_from_settings(settings: Settings) -> StrapiClient:
    return StrapiClient(settings.strapi)


def collection_endpoint(settings: Settings, name: str) -> str:
    collection = settings.collections.get(name)
    if not collection or collection.owner != "strapi" or not collection.strapi_endpoint:
        raise StrapiError(f"collection {name} is not configured for Strapi")
    return collection.strapi_endpoint


def normalize_entity(entity: dict[str, Any]) -> dict[str, Any]:
    attributes = entity.get("attributes")
    if isinstance(attributes, dict):
        return {"id": entity.get("id"), **attributes}
    return dict(entity)


def entity_reference(entity: dict[str, Any]) -> str | int:
    value = entity.get("documentId") or entity.get("id")
    if value is None:
        raise StrapiError("Strapi entity has no documentId or id")
    return value


def flatten_query(values: dict[str, Any]) -> dict[str, str]:
    result = {}
    for key, value in values.items():
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        elif value is not None:
            result[key] = str(value)
    return result
