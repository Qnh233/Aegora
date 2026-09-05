from .models import MCPConnectionDefinition, ToolDefinition, WorkflowCapabilityRequest


def build_workflow_capability(
    workflow_id: str,
    request: WorkflowCapabilityRequest,
    connection: MCPConnectionDefinition,
) -> ToolDefinition:
    """Build a governed workflow manifest while reusing MCP as the transport."""
    return ToolDefinition(
        tool_id=workflow_id,
        name=request.name,
        description=request.description,
        status=request.status,
        source="workflow",
        runner_tool_id=connection.runner_tool_id,
        runner_name=request.runner_name,
        mcp_connection_id=request.mcp_connection_id,
        mcp_connection=connection,
        version=request.version,
        read_only=False,
        idempotent=False,
        parallel_safe=False,
        requires_approval=request.requires_approval,
        side_effect_level=request.side_effect_level,
        data_sensitivity=request.data_sensitivity,
        network_access="internal_only",
        layer="workflow",
        category="workflow",
        namespace="workflow",
        group_id="workflow.registered",
        group_name="Registered Workflows",
        timeout_ms=request.timeout_ms,
        input_schema=request.input_schema,
        scope_schema=request.scope_schema,
        scope_descriptions=request.scope_descriptions,
    )
