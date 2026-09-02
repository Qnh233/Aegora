from __future__ import annotations

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, Sequence

from aegora_runtime.config import EmbeddingSettings


class Encoder(Protocol):
    dimension: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class RemoteEmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingJob:
    faq_id: int
    strapi_id: int
    faq_version: int
    content_hash: str
    text: str


class BgeM3Encoder:
    def __init__(self, settings: EmbeddingSettings):
        import torch
        from sentence_transformers import SentenceTransformer

        if not settings.local_path.exists():
            raise FileNotFoundError(f"BGE-M3 local model not found: {settings.local_path}")
        torch.set_num_threads(max(1, os.cpu_count() or 1))
        self.model = SentenceTransformer(
            str(settings.local_path),
            local_files_only=True,
        )
        self.model.max_seq_length = settings.max_length
        self.dimension = int(self.model.get_sentence_embedding_dimension())
        self.batch_size = settings.batch_size

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self.model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return vectors.tolist()


class SiliconFlowBgeM3Encoder:
    """OpenAI-compatible SiliconFlow embedding client."""

    def __init__(self, settings: EmbeddingSettings):
        if not settings.api_key:
            raise RemoteEmbeddingError("EMBEDDING_API_KEY is required for siliconflow")
        self.settings = settings
        self.dimension = settings.dimension
        self.batch_size = settings.batch_size

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        values = [str(text) for text in texts]
        if not values:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(values), self.batch_size):
            vectors.extend(self._encode_batch(values[start : start + self.batch_size]))
        return vectors

    def _encode_batch(self, texts: list[str]) -> list[list[float]]:
        last_error: RemoteEmbeddingError | None = None
        for attempt in range(1, self.settings.max_retries + 1):
            try:
                return self._request_batch(texts)
            except RemoteEmbeddingError as exc:
                last_error = exc
                if attempt < self.settings.max_retries:
                    time.sleep(min(attempt, 3))
        assert last_error is not None
        raise last_error

    def _request_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {
            "model": self.settings.model,
            "input": texts,
            "encoding_format": "float",
        }
        request = urllib.request.Request(
            f"{self.settings.base_url}/embeddings",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RemoteEmbeddingError(f"SiliconFlow HTTP {exc.code}: {detail[:500]}") from exc
        except TimeoutError as exc:
            raise RemoteEmbeddingError(f"SiliconFlow request timed out: {exc}") from exc
        except http.client.IncompleteRead as exc:
            raise RemoteEmbeddingError(f"SiliconFlow response incomplete: {exc}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RemoteEmbeddingError(f"SiliconFlow request failed: {exc}") from exc

        try:
            data = json.loads(body)["data"]
            ordered = sorted(data, key=lambda item: int(item["index"]))
            vectors = [normalize_vector(item["embedding"]) for item in ordered]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RemoteEmbeddingError(f"Unexpected SiliconFlow response: {body[:500]}") from exc
        if len(vectors) != len(texts):
            raise RemoteEmbeddingError(f"SiliconFlow returned {len(vectors)} vectors for {len(texts)} texts")
        for vector in vectors:
            if len(vector) != self.dimension:
                raise RemoteEmbeddingError(
                    f"SiliconFlow vector dimension={len(vector)}, expected={self.dimension}"
                )
        return vectors


def build_encoder(settings: EmbeddingSettings) -> Encoder:
    if settings.provider == "local":
        return BgeM3Encoder(settings)
    if settings.provider == "siliconflow":
        return SiliconFlowBgeM3Encoder(settings)
    raise ValueError(f"unsupported embedding provider: {settings.provider}")


def normalize_vector(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values] if norm else values


def build_embedding_text(
    title: str | None,
    faq: str,
    keywords: str | None,
    response: str,
) -> str:
    parts = []
    if title:
        parts.append(f"标题：{title.strip()}")
    parts.append(f"问题：{faq.strip()}")
    if keywords:
        parts.append(f"关键词：{keywords.strip()}")
    parts.append(f"回答：{response.strip()}")
    return "\n".join(parts)


def vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
