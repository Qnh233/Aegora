from app import workflow_capabilities
from app.models import MCPConnectionDefinition, WorkflowCapabilityRequest


def test_workflow_capability_binds_existing_mcp_connection() -> None:
    connection = MCPConnectionDefinition(
        connection_id="workflow.ops",
        name="Workflow MCP",
        status="active",
        transport="streamable_http",
        config={"url": "https://workflow.example.test/mcp"},
        config_version=1,
        config_hash="sha256:test",
    )
    workflow = workflow_capabilities.build_workflow_capability(
        "expense.submit",
        WorkflowCapabilityRequest(
            name="Submit Expense",
            description="提交报销审批工作流",
            mcp_connection_id="workflow.ops",
            runner_name="submit_expense",
            requires_approval=True,
            input_schema={
                "type": "object",
                "required": ["amount"],
                "properties": {"amount": {"type": "number"}},
            },
            scope_schema={"department": ["finance"]},
        ),
        connection,
    )

    assert workflow.source == "workflow"
    assert workflow.layer == "workflow"
    assert workflow.runner_tool_id == connection.runner_tool_id
    assert workflow.runner_name == "submit_expense"
    assert workflow.mcp_connection_id == "workflow.ops"
    assert workflow.requires_approval is True
