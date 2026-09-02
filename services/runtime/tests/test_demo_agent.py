from __future__ import annotations

from unittest.mock import patch

from aegora_runtime.config import load_settings
from aegora_runtime.demo_agent import rrf_retrieve
from aegora_runtime.embeddings import RemoteEmbeddingError


class FakeEncoder:
    def encode(self, texts):
        return [[0.1] * 1024 for _ in texts]


class FailedRemoteEncoder:
    def encode(self, texts):
        raise RemoteEmbeddingError("timeout")


def test_rrf_retrieve_encodes_query_and_calls_hybrid_search() -> None:
    settings = load_settings(env_path=None)
    expected = [{"faq_id": 1}]

    with patch("aegora_runtime.demo_agent.hybrid_retrieve", return_value=expected) as retrieve:
        result = rrf_retrieve("如何设置悬浮窗", 5, FakeEncoder(), settings)

    assert result == expected
    retrieve.assert_called_once_with(
        "如何设置悬浮窗",
        [0.1] * 1024,
        top_k=5,
        settings=settings,
    )


def test_rrf_retrieve_skips_ambiguous_query() -> None:
    settings = load_settings(env_path=None)

    with patch("aegora_runtime.demo_agent.hybrid_retrieve") as retrieve:
        result = rrf_retrieve("下载", 5, FakeEncoder(), settings)

    assert result == []
    retrieve.assert_not_called()


def test_rrf_retrieve_falls_back_to_fulltext_when_remote_embedding_fails() -> None:
    settings = load_settings(env_path=None)
    lexical = [{"faq_id": 1, "score": 0.8}]

    with patch("aegora_runtime.demo_agent.fulltext_retrieve", return_value=lexical):
        result = rrf_retrieve("如何设置悬浮窗", 5, FailedRemoteEncoder(), settings)

    assert [item["faq_id"] for item in result] == [1]
    assert result[0]["source_ranks"] == {"fulltext": 1}
