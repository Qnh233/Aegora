import pytest

from app.authz import (
    effective_tools,
    merge_tool_scopes,
    scope_is_subset,
    validate_configured_tools,
    validate_tool_scope_subset,
)


def test_agent_configuration_must_not_exceed_creator_permissions():
    with pytest.raises(PermissionError, match="sql_readonly"):
        validate_configured_tools({"kb_read", "sql_readonly"}, {"kb_read"})


def test_effective_tools_decay_after_role_permission_is_removed():
    assert effective_tools(
        {"kb_read", "sql_readonly"},
        {"kb_read", "sql_readonly"},
        {"kb_read"},
    ) == {"kb_read"}


def test_new_role_permission_does_not_expand_existing_agent():
    assert effective_tools(
        {"kb_read"},
        {"kb_read"},
        {"kb_read", "sql_readonly"},
    ) == {"kb_read"}


def test_merge_tool_scopes_unions_role_permissions():
    assert merge_tool_scopes(
        [
            ("calculator", {"actions": ["calculate"]}),
            ("calculator", {"actions": ["read"]}),
            ("calculator", {"actions": ["calculate"]}),
        ]
    ) == {
        "calculator": {"actions": ["calculate", "read"]},
    }


def test_merge_tool_scopes_rejects_unsupported_scope_shape():
    with pytest.raises(ValueError, match="scope"):
        merge_tool_scopes([("calculator", {"actions": "calculate"})])


def test_scope_subset_requires_every_key_and_value_to_be_allowed():
    allowed = {"actions": ["calculate", "read"]}

    assert scope_is_subset({"actions": ["calculate"]}, allowed)
    assert not scope_is_subset({"actions": ["delete"]}, allowed)
    assert not scope_is_subset({"schemas": ["finance"]}, allowed)


def test_validate_tool_scope_subset_reports_tool_and_scope():
    with pytest.raises(PermissionError, match="calculator.*actions"):
        validate_tool_scope_subset(
            "calculator",
            {"actions": ["delete"]},
            {"actions": ["calculate"]},
        )
