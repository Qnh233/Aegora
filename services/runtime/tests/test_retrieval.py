from __future__ import annotations

import unittest

from aegora_runtime.retrieval import reciprocal_rank_fusion, trusted_top1_images, vector_retrieve


class RetrievalTest(unittest.TestCase):
    def test_vector_retrieve_rejects_wrong_dimension(self) -> None:
        with self.assertRaises(ValueError):
            vector_retrieve([0.1, 0.2])

    def test_reciprocal_rank_fusion_combines_rankings(self) -> None:
        fulltext = [
            {"faq_id": 1, "title": "A", "score": 0.9},
            {"faq_id": 2, "title": "B", "score": 0.8},
        ]
        vector = [
            {"faq_id": 2, "title": "B", "score": 0.7},
            {"faq_id": 3, "title": "C", "score": 0.6},
        ]

        results = reciprocal_rank_fusion(
            {"fulltext": fulltext, "vector": vector},
            top_k=3,
            rrf_k=60,
        )

        self.assertEqual([item["faq_id"] for item in results], [2, 1, 3])
        self.assertEqual(results[0]["source_ranks"], {"fulltext": 2, "vector": 1})
        self.assertAlmostEqual(results[0]["score"], 1 / 62 + 1 / 61)

    def test_reciprocal_rank_fusion_rejects_invalid_parameters(self) -> None:
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion({}, top_k=0, rrf_k=60)
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion({}, top_k=5, rrf_k=0)

    def test_reciprocal_rank_fusion_uses_source_rank_to_break_ties(self) -> None:
        fulltext = [{"faq_id": 1, "score": 0.9}, {"faq_id": 2, "score": 0.8}]
        vector = [{"faq_id": 2, "score": 0.9}, {"faq_id": 1, "score": 0.8}]

        results = reciprocal_rank_fusion(
            {"fulltext": fulltext, "vector": vector},
            top_k=2,
            rrf_k=60,
            tie_break_source="vector",
        )

        self.assertEqual([item["faq_id"] for item in results], [2, 1])

    def test_trusted_top1_images_requires_dual_source_faq_answer(self) -> None:
        faqs = [
            {
                "faq_id": 43,
                "source_ranks": {"fulltext": 1, "vector": 1},
                "response_pic_app_url": "https://cdn.example/app.png",
                "response_pic_pc_url": "https://cdn.example/pc.png",
            }
        ]

        self.assertEqual(
            trusted_top1_images(faqs, route="faq_answer"),
            [
                {"faq_id": 43, "platform": "APP", "url": "https://cdn.example/app.png"},
                {"faq_id": 43, "platform": "PC", "url": "https://cdn.example/pc.png"},
            ],
        )
        self.assertEqual(trusted_top1_images(faqs, route="clarify"), [])

    def test_trusted_top1_images_rejects_single_source_and_unsafe_urls(self) -> None:
        single_source = [
            {
                "faq_id": 43,
                "source_ranks": {"vector": 1},
                "response_pic_app_url": "https://cdn.example/app.png",
            }
        ]
        unsafe = [
            {
                "faq_id": 43,
                "source_ranks": {"fulltext": 1, "vector": 1},
                "response_pic_app_url": "javascript:alert(1)",
            }
        ]

        self.assertEqual(trusted_top1_images(single_source, route="faq_answer"), [])
        self.assertEqual(trusted_top1_images(unsafe, route="faq_answer"), [])


if __name__ == "__main__":
    unittest.main()
