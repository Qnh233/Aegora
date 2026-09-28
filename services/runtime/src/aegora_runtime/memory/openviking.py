from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from aegora_runtime.config import Settings
from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite


LOGGER = logging.getLogger(__name__)
_SCHEMA = "aegora.memory.v1"
_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


class OpenVikingError(RuntimeError):
    pass


class OpenVikingNotFound(OpenVikingError):
    pass


class OpenVikingHttpClient:
    """Small dependency-free client for the current OpenViking HTTP API."""

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.memory.openviking_base_url.rstrip("/")
        self.api_key = settings.memory.openviking_api_key
        self.auth_mode = settings.memory.openviking_auth_mode
        self.timeout = settings.memory.openviking_timeout_seconds

    def request(
        self,
        *,
        scope: MemoryScope,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            encoded = urlencode(
                {
                    key: _query_value(value)
                    for key, value in query.items()
                    if value is not None
                }
            )
            if encoded:
                url = f"{url}?{encoded}"

        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if self.api_key:
            headers["X-API-Key"] = self.api_key

        if self.auth_mode == "trusted":
            headers["X-OpenViking-Account"] = scope.tenant_id or "default"
            headers["X-OpenViking-User"] = scope.user_id
            if scope.agent_id:
                headers["X-OpenViking-Actor-Peer"] = scope.agent_id

        request = Request(url=url, data=body, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            if exc.code == 404:
                raise OpenVikingNotFound(str(exc)) from exc
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise OpenVikingError(f"OpenViking HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OpenVikingError(f"OpenViking request failed: {exc}") from exc

        if not raw:
            return None
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenVikingError("OpenViking returned invalid JSON") from exc

        if isinstance(envelope, dict) and envelope.get("status") not in (None, "ok"):
            raise OpenVikingError(
                f"OpenViking status={envelope.get('status')}: {envelope.get('message') or envelope.get('error')}"
            )
        if isinstance(envelope, dict) and "result" in envelope:
            return envelope["result"]
        return envelope


class OpenVikingMemoryProvider:
    """OpenViking adapter. Aegora remains owner of governance and memory policy."""

    name = "openviking"

    def __init__(
        self,
        settings: Settings,
        client: OpenVikingHttpClient | Any | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or OpenVikingHttpClient(settings)
        self.root_uri = settings.memory.openviking_root_uri.rstrip("/")
        self.wait_for_index = settings.memory.openviking_wait_for_index

    def recall(
        self,
        *,
        scope: MemoryScope,
        query: str,
        limit: int,
    ) -> list[MemoryItem]:
        root = self._category_root(scope)
        result = self.client.request(
            scope=scope,
            method="POST",
            path="/api/v1/search/find",
            payload={
                "query": query,
                "target_uri": f"{root}/",
                "context_type": ["memory"],
                "limit": max(1, limit),
            },
        )
        items: list[MemoryItem] = []
        for hit in _flatten_hits(result):
            uri = str(hit.get("uri") or "").strip()
            if not uri or uri.endswith(("/.overview.md", "/.abstract.md")):
                continue
            try:
                item = self._read_item(scope=scope, uri=uri)
            except OpenVikingError:
                LOGGER.warning("skipping non-Aegora OpenViking recall hit: %s", uri)
                continue
            if item is None or not _visible_to_scope(item, scope):
                continue
            if _expired(item):
                continue
            score = hit.get("score")
            if score is not None:
                metadata = dict(item.metadata)
                metadata["provider_score"] = score
                item = MemoryItem(
                    memory_id=item.memory_id,
                    memory_type=item.memory_type,
                    key=item.key,
                    content=item.content,
                    scope=item.scope,
                    namespace=item.namespace,
                    version=item.version,
                    source_agent_id=item.source_agent_id,
                    source_kind=item.source_kind,
                    metadata=metadata,
                )
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def get_current(
        self,
        *,
        scope: MemoryScope,
        key: str,
    ) -> MemoryItem | None:
        uri = self._memory_uri(scope, key)
        return self._read_item(scope=scope, uri=uri, allow_not_found=True)

    def remember(
        self,
        *,
        scope: MemoryScope,
        memory: MemoryWrite,
    ) -> MemoryItem:
        current = self.get_current(scope=scope, key=memory.key)
        if (
            current is not None
            and current.content == memory.value
            and _metadata_iso(current, "valid_until") == _iso(memory.expires_at)
        ):
            return current

        version = current.version + 1 if current else 1
        supersedes_id = current.memory_id if current else None
        uri = self._memory_uri(scope, memory.key)
        item = MemoryItem(
            memory_id=_scoped_memory_id(scope, uri),
            memory_type=memory.memory_type,
            key=memory.key,
            content=memory.value,
            scope=scope.memory_scope,
            namespace=scope.namespace,
            version=version,
            source_agent_id=memory.source_agent_id or scope.agent_id,
            source_kind=memory.source_kind,
            metadata={
                "tenant_id": scope.tenant_id or "default",
                "user_id": scope.user_id,
                "agent_id": scope.agent_id if scope.memory_scope == "user_agent" else None,
                "source_session_id": memory.source_session_id or scope.session_id,
                "source_trace_id": memory.source_trace_id,
                "confidence": memory.confidence,
                "importance": memory.importance,
                "supersedes_id": supersedes_id,
                "valid_from": datetime.now(timezone.utc).isoformat(),
                "valid_until": _iso(memory.expires_at),
                "reason": memory.reason,
                "provider": self.name,
                "provider_uri": uri,
            },
        )
        self.client.request(
            scope=scope,
            method="POST",
            path="/api/v1/content/write",
            payload={
                "uri": uri,
                "content": json.dumps(_item_to_document(item), ensure_ascii=False, sort_keys=True),
                "mode": "replace",
                "wait": self.wait_for_index,
                "tags": self._tags(scope),
                "tag_mode": "replace",
            },
        )
        return item

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
        reason: str | None = None,
        source_trace_id: str | None = None,
        source_kind: str = "explicit",
    ) -> bool:
        del reason, source_trace_id, source_kind
        uri = self._memory_uri(scope, key)
        current = self._read_item(scope=scope, uri=uri, allow_not_found=True)
        if current is None:
            return False
        self.client.request(
            scope=scope,
            method="DELETE",
            path="/api/v1/fs",
            query={"uri": uri, "recursive": "false"},
        )
        return True

    def expire_due(
        self,
        *,
        tenant_id: str | None = None,
        limit: int = 500,
    ) -> int:
        # OpenViking's public user filesystem is identity-scoped. This provider
        # cannot safely sweep every user with only tenant_id. Expired documents
        # are therefore filtered on recall. A future user-scoped cleanup job can
        # physically remove them without weakening tenant/user isolation.
        del tenant_id, limit
        return 0

    def health(self, *, scope: MemoryScope) -> bool:
        try:
            result = self.client.request(scope=scope, method="GET", path="/health")
        except OpenVikingError:
            return False
        if isinstance(result, dict):
            return bool(result.get("healthy", True))
        return True

    def _read_item(
        self,
        *,
        scope: MemoryScope,
        uri: str,
        allow_not_found: bool = False,
    ) -> MemoryItem | None:
        try:
            content = self.client.request(
                scope=scope,
                method="GET",
                path="/api/v1/content/read",
                query={"uri": uri},
            )
        except OpenVikingNotFound:
            if allow_not_found:
                return None
            raise
        if not isinstance(content, str):
            if isinstance(content, dict) and isinstance(content.get("content"), str):
                content = content["content"]
            else:
                raise OpenVikingError("OpenViking content/read did not return text")
        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenVikingError(f"invalid Aegora memory document at {uri}") from exc
        return _document_to_item(raw, uri=uri)

    def _category_root(self, scope: MemoryScope) -> str:
        category = "preferences" if scope.namespace == "preferences" else "entities"
        return f"{self.root_uri}/{category}"

    def _memory_uri(self, scope: MemoryScope, key: str) -> str:
        root = self._category_root(scope)
        namespace = _safe_segment(scope.namespace)
        if scope.memory_scope == "user_agent":
            scope_segment = f"user_agent-{_digest(scope.agent_id or '')}"
        else:
            scope_segment = scope.memory_scope
        return f"{root}/aegora/{namespace}/{scope_segment}/{_safe_segment(key)}.md"

    def _tags(self, scope: MemoryScope) -> list[str]:
        tags = [
            "aegora=memory",
            f"tenant={_digest(scope.tenant_id or 'default')}",
            f"user={_digest(scope.user_id)}",
            f"namespace={_digest(scope.namespace)}",
            f"scope={scope.memory_scope}",
        ]
        if scope.agent_id:
            tags.append(f"agent={_digest(scope.agent_id)}")
        return tags


def _item_to_document(item: MemoryItem) -> dict[str, Any]:
    return {
        "schema": _SCHEMA,
        "memory_id": item.memory_id,
        "memory_type": item.memory_type,
        "key": item.key,
        "content": item.content,
        "scope": item.scope,
        "namespace": item.namespace,
        "version": item.version,
        "source_agent_id": item.source_agent_id,
        "source_kind": item.source_kind,
        "metadata": dict(item.metadata),
    }


def _document_to_item(raw: dict[str, Any], *, uri: str) -> MemoryItem:
    if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
        raise OpenVikingError(f"unsupported Aegora memory document at {uri}")
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return MemoryItem(
        memory_id=str(raw.get("memory_id") or uri),
        memory_type=str(raw.get("memory_type") or "semantic"),
        key=str(raw.get("key") or ""),
        content=str(raw.get("content") or ""),
        scope=str(raw.get("scope") or "user_global"),
        namespace=str(raw.get("namespace") or "preferences"),
        version=int(raw.get("version") or 1),
        source_agent_id=(
            str(raw["source_agent_id"])
            if raw.get("source_agent_id") not in (None, "")
            else None
        ),
        source_kind=str(raw.get("source_kind") or "legacy"),
        metadata=dict(metadata),
    )


def _visible_to_scope(item: MemoryItem, scope: MemoryScope) -> bool:
    if str(item.metadata.get("tenant_id") or "default") != str(scope.tenant_id or "default"):
        return False
    if str(item.metadata.get("user_id") or "") != scope.user_id:
        return False
    if item.scope == "user_agent":
        return bool(
            scope.agent_id
            and str(item.metadata.get("agent_id") or "") == str(scope.agent_id)
        )
    return item.scope in {"user_global", "tenant_user"}


def _expired(item: MemoryItem) -> bool:
    value = item.metadata.get("valid_until")
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _flatten_hits(result: Any) -> list[dict[str, Any]]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []

    hits: list[dict[str, Any]] = []
    for key in ("memories", "entries", "results", "contexts", "items", "resources"):
        value = result.get(key)
        if isinstance(value, list):
            hits.extend(item for item in value if isinstance(item, dict))
    if not hits and "uri" in result:
        hits.append(result)
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for hit in hits:
        uri = str(hit.get("uri") or "")
        if uri and uri in seen:
            continue
        if uri:
            seen.add(uri)
        deduped.append(hit)
    return deduped


def _scoped_memory_id(scope: MemoryScope, uri: str) -> str:
    tenant = _digest(scope.tenant_id or "default")
    user = _digest(scope.user_id)
    return f"openviking:{tenant}:{user}:{_digest(uri, length=16)}"


def _safe_segment(value: str) -> str:
    text = _SAFE.sub("-", str(value or "").strip()).strip("-._")[:48] or "memory"
    return f"{text}-{_digest(value, length=8)}"


def _digest(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:length]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _metadata_iso(item: MemoryItem, key: str) -> str | None:
    value = item.metadata.get(key)
    return str(value) if value else None


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
