from __future__ import annotations

from scripts.generate_retrieval_holdout import build_holdout, choose_holdout_query, validate_isolation


def knowledge_row(row_id: int, faq: str, title: str, category: str = "功能设置") -> dict:
    return {
        "id": row_id,
        "FAQ": faq,
        "Title": title,
        "Response": f"回答 {row_id}",
        "Keywords": "",
        "Intention": "",
        "Category": category,
    }


def test_holdout_excludes_development_duplicate_groups() -> None:
    duplicate = knowledge_row(2, "开发集问题", "开发集问题")
    duplicate["Response"] = "回答 1"
    rows = [
        knowledge_row(1, "开发集问题", "开发集问题"),
        duplicate,
        knowledge_row(3, "候选问题一", "候选标题一"),
        knowledge_row(4, "候选问题二", "候选标题二"),
    ]
    development = [{"expected_faq_ids": [1]}]

    holdout = build_holdout(rows, development, size=2, per_category=1, seed=1)

    assert {faq_id for row in holdout for faq_id in row["expected_faq_ids"]} == {3, 4}
    assert validate_isolation(rows, development, holdout)["content_hash_overlap"] == 0


def test_choose_holdout_query_prefers_alternate_question() -> None:
    row = knowledge_row(1, "如何设置悬浮窗\n苹果手机悬浮窗怎么设置", "如何设置悬浮窗？")

    query, source = choose_holdout_query(row)

    assert query == "苹果手机悬浮窗怎么设置"
    assert source == "alternate_query"
