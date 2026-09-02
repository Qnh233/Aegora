from __future__ import annotations

from scripts.expand_eval_duplicate_ids import expand_expected_faq_ids


def test_expand_expected_faq_ids_adds_all_exact_duplicates() -> None:
    rows = [
        {"expected_faq_ids": [710]},
        {"expected_faq_ids": [1303, 1325]},
        {"expected_faq_ids": []},
    ]
    duplicate_map = {
        710: [710, 726],
        726: [710, 726],
        1303: [1303, 1325],
        1325: [1303, 1325],
    }

    changed_rows, added_ids = expand_expected_faq_ids(rows, duplicate_map)

    assert changed_rows == 1
    assert added_ids == 1
    assert rows[0]["expected_faq_ids"] == [710, 726]
    assert rows[1]["expected_faq_ids"] == [1303, 1325]
    assert rows[2]["expected_faq_ids"] == []
