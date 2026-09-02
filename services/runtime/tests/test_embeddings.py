from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import pytest

from aegora_runtime.config import EmbeddingSettings
from aegora_runtime.embeddings import (
    RemoteEmbeddingError,
    SiliconFlowBgeM3Encoder,
    build_embedding_text,
    vector_literal,
)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def remote_settings(api_key: str | None = "sk-test") -> EmbeddingSettings:
    from pathlib import Path

    return EmbeddingSettings(
        provider="siliconflow",
        api_key=api_key,
        base_url="https://api.siliconflow.cn/v1",
        model="BAAI/bge-m3",
        local_path=Path("/unused"),
        dimension=3,
        timeout_seconds=10,
        batch_size=8,
        max_retries=1,
        max_length=1024,
        enable_startup_warmup=False,
    )


class EmbeddingTest(unittest.TestCase):
    def test_build_embedding_text_contains_structured_fields(self) -> None:
        text = build_embedding_text("设置悬浮窗", "如何设置悬浮窗", "悬浮窗 实时盯盘", "进入实时盯盘设置")

        self.assertIn("标题：设置悬浮窗", text)
        self.assertIn("问题：如何设置悬浮窗", text)
        self.assertIn("关键词：悬浮窗 实时盯盘", text)
        self.assertIn("回答：进入实时盯盘设置", text)

    def test_vector_literal(self) -> None:
        self.assertEqual(vector_literal([0.1, -0.2]), "[0.10000000,-0.20000000]")


def test_siliconflow_encoder_calls_openai_compatible_api_and_orders_vectors() -> None:
    response = FakeResponse(
        {
            "data": [
                {"index": 1, "embedding": [0.0, 2.0, 0.0]},
                {"index": 0, "embedding": [3.0, 0.0, 0.0]},
            ]
        }
    )
    encoder = SiliconFlowBgeM3Encoder(remote_settings())

    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        vectors = encoder.encode(["问题一", "问题二"])

    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    assert request.full_url == "https://api.siliconflow.cn/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert payload == {"model": "BAAI/bge-m3", "input": ["问题一", "问题二"], "encoding_format": "float"}
    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def test_siliconflow_encoder_requires_api_key() -> None:
    with pytest.raises(RemoteEmbeddingError, match="EMBEDDING_API_KEY"):
        SiliconFlowBgeM3Encoder(remote_settings(api_key=None))


def test_siliconflow_encoder_rejects_unexpected_dimension() -> None:
    response = FakeResponse({"data": [{"index": 0, "embedding": [1.0, 0.0]}]})
    encoder = SiliconFlowBgeM3Encoder(remote_settings())

    with patch("urllib.request.urlopen", return_value=response):
        with pytest.raises(RemoteEmbeddingError, match="dimension"):
            encoder.encode(["问题"])


if __name__ == "__main__":
    unittest.main()
