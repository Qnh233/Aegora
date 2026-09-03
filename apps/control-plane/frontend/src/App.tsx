import {
  Alert,
  Button,
  Checkbox,
  Collapse,
  ConfigProvider,
  Form,
  Input,
  InputNumber,
  Layout,
  Modal,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message as toast
} from "antd";
import {
  AppstoreOutlined,
  CalculatorOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloseCircleOutlined,
  CopyOutlined,
  DesktopOutlined,
  EditOutlined,
  ExclamationCircleOutlined,
  FileTextOutlined,
  HistoryOutlined,
  LoginOutlined,
  GlobalOutlined,
  LockOutlined,
  MinusCircleOutlined,
  LogoutOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  PoweroffOutlined,
  ReloadOutlined,
  RobotOutlined,
  SafetyCertificateOutlined,
  SettingOutlined,
  TeamOutlined,
  ThunderboltOutlined,
  UserOutlined
} from "@ant-design/icons";
import { useEffect, useMemo, useState } from "react";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const DEFAULT_USER = "u_console";
const DEFAULT_MODEL = import.meta.env.VITE_DEFAULT_MODEL ?? "";
const DEFAULT_RUNNER_BACKEND = import.meta.env.VITE_RUNNER_BACKEND ?? "debug";
const ICON_OPTIONS = [
  { label: "机器人", value: "robot" },
  { label: "计算器", value: "calculator" },
  { label: "时钟", value: "time" },
  { label: "文档", value: "text" },
  { label: "控制台", value: "desktop" }
];

type View = "plaza" | "debug" | "setup" | "tools" | "access" | "agents" | "runs" | "profile";

type Agent = {
  agent_id: string;
  name: string;
  icon: string;
  owner_user_id: string;
  visibility: "private" | "public";
  members: string[];
  enabled: boolean;
  tools: string[];
};

type AgentDetail = Agent & {
  system_prompt: string;
  model: string | null;
  tool_configs: Array<{ tool_id: string; scope: Record<string, string[]> }>;
  channels: string[];
};

type ToolCall = {
  tool_id: string;
  status: string;
  result: string;
};

type ApprovalRequest = {
  approval_id?: string;
  id?: string;
  tool_id?: string;
  tool_name?: string;
  name?: string;
  reason?: string;
  summary?: string;
  input?: unknown;
  arguments?: unknown;
  payload?: unknown;
  [key: string]: unknown;
};

type Run = {
  run_id: string;
  agent_id: string | null;
  actor_id: string | null;
  message: string;
  answer: string | null;
  status: string;
  created_at: string;
};

type RunResult = {
  run_id: string;
  answer: string;
  tool_calls: ToolCall[];
  status?: string;
  approval_requests?: ApprovalRequest[];
};

type ReleaseToolConfig = {
  tool_id: string;
  runner_tool_id?: string | null;
  scope?: Record<string, string[]>;
};

type AgentRelease = {
  release_id: string;
  agent_id: string;
  version: number;
  status: string;
  config_json: {
    agent?: Record<string, unknown>;
    tools?: ReleaseToolConfig[];
  };
  published_by: string;
  published_at: string;
  revoked_at: string | null;
};

type UserProfile = {
  user_id: string;
  status: string;
  roles: string[];
  role_tool_scopes: Record<string, Record<string, string[]>>;
  is_platform_admin: boolean;
};

type LoginResponse = {
  access_token: string;
  token_type: string;
  user: UserProfile;
};

type AuthConfig = {
  auth_mode: "local" | "oa";
  oa_login_url?: string | null;
};

type Role = {
  role_id: string;
  name: string;
  description: string;
  status: string;
  is_system: boolean;
  permissions: Array<{ tool_id: string; scope: Record<string, string[]> }>;
};

type ToolDefinition = {
  tool_id: string;
  name: string;
  description: string;
  status: string;
  source: "mcp" | "http" | "workflow" | "workflow_agent" | "local";
  runner_tool_id: string | null;
  runner_name: string | null;
  mcp_connection_id: string | null;
  mcp_connection: MCPConnection | null;
  version: string;
  read_only: boolean;
  idempotent: boolean;
  parallel_safe: boolean;
  requires_approval: boolean;
  side_effect_level: string;
  data_sensitivity: string;
  network_access: string;
  layer: "runtime" | "support" | "business" | "integration" | "workflow" | string;
  category: string;
  namespace: string;
  group_id: string;
  group_name: string;
  timeout_ms: number;
  input_schema: Record<string, unknown>;
  scope_schema: Record<string, string[]>;
  scope_descriptions: Record<string, string>;
  manifest_hash: string;
};

type MCPConnection = {
  connection_id: string;
  name: string;
  status: "active" | "disabled";
  transport: "streamable_http" | "stdio";
  config: {
    url?: string;
    command?: string;
    args?: string[];
    cwd?: string;
    env_vars?: string[];
    bearer_env?: string;
    headers?: Record<string, string>;
    connect_timeout_ms?: number;
    call_timeout_ms?: number;
    idle_ttl_seconds?: number;
    discovery_ttl_seconds?: number;
  };
  config_version: number;
  config_hash: string;
};

type MCPDiscoveryResult = {
  connection_id: string;
  discovered_count: number;
  tool_ids: string[];
  disabled_tool_ids: string[];
};

type AdminUser = {
  user_id: string;
  status: string;
  roles: string[];
};

function toolIcon(toolId: string) {
  if (toolId === "calculator") return <CalculatorOutlined />;
  if (toolId === "time_now") return <ClockCircleOutlined />;
  if (toolId === "text_stats") return <FileTextOutlined />;
  return <ThunderboltOutlined />;
}

function agentIcon(agent: Agent) {
  if (agent.icon === "calculator") return <CalculatorOutlined />;
  if (agent.icon === "time") return <ClockCircleOutlined />;
  if (agent.icon === "text") return <FileTextOutlined />;
  if (agent.icon === "desktop") return <DesktopOutlined />;
  if (agent.tools.includes("calculator")) return <CalculatorOutlined />;
  if (agent.tools.includes("time_now")) return <ClockCircleOutlined />;
  if (agent.tools.includes("text_stats")) return <FileTextOutlined />;
  return <RobotOutlined />;
}

function visibilityTag(agent: Agent) {
  return agent.visibility === "public" ? (
    <Tag icon={<GlobalOutlined />} color="cyan">公开</Tag>
  ) : (
    <Tag icon={<LockOutlined />} color="purple">私有</Tag>
  );
}

function sourceColor(source: ToolDefinition["source"]) {
  if (source === "local") return "green";
  if (source === "mcp") return "geekblue";
  if (source === "workflow") return "purple";
  if (source === "workflow_agent") return "magenta";
  return "blue";
}

function sourceLabel(source: ToolDefinition["source"]) {
  if (source === "local") return "本地工具";
  if (source === "mcp") return "MCP 工具";
  if (source === "workflow") return "工作流能力";
  if (source === "workflow_agent") return "工作流 Agent";
  return "HTTP 工具";
}

function layerLabel(layer: string) {
  if (layer === "runtime") return "Runtime 通用层";
  if (layer === "support") return "Support 客服层";
  if (layer === "business") return "业务领域层";
  if (layer === "integration") return "外部集成层";
  if (layer === "workflow") return "工作流层";
  return layer || "未分层";
}

function layerColor(layer: string) {
  if (layer === "runtime") return "cyan";
  if (layer === "support") return "blue";
  if (layer === "business") return "blue";
  if (layer === "integration") return "geekblue";
  if (layer === "workflow") return "magenta";
  return "default";
}

function statusLabel(status: string) {
  if (status === "active") return "启用";
  if (status === "disabled") return "停用";
  if (status === "succeeded") return "成功";
  if (status === "failed") return "失败";
  if (status === "running") return "运行中";
  if (status === "pending_approval") return "等待审批";
  if (status === "approved") return "已同意";
  if (status === "rejected") return "已拒绝";
  if (status === "published") return "已发布";
  if (status === "revoked") return "已撤销";
  return status || "未知";
}

function toolName(tool?: Pick<ToolDefinition, "tool_id" | "name">, fallbackId?: string) {
  const toolId = tool?.tool_id ?? fallbackId ?? "";
  const knownNames: Record<string, string> = {
    calculator: "计算器",
    time_now: "当前时间",
    text_stats: "文本统计"
  };
  return knownNames[toolId] ?? tool?.name ?? toolId;
}

function toolDescription(tool: ToolDefinition) {
  if (tool.description) return tool.description;
  if (tool.source === "mcp") {
    const connectionName = tool.mcp_connection?.name ?? tool.mcp_connection_id ?? "MCP 连接";
    return `来自 ${connectionName} 的 MCP 工具，远端名称 ${tool.runner_name ?? tool.tool_id}`;
  }
  return "该工具暂未提供描述，请以来源、运行属性和参数 schema 判断使用边界。";
}

function categoryLabel(category: string) {
  if (category === "utility") return "通用能力";
  if (category === "general") return "通用";
  return category || "未分类";
}

function scopeKeyLabel(key: string) {
  const labels: Record<string, string> = {
    actions: "操作",
    resources: "资源",
    product_ids: "产品",
    domains: "业务领域",
    skill_library_ids: "经验库",
    faq_collections: "FAQ 知识库",
    memory_namespaces: "记忆空间",
    handoff_queues: "转人工队列"
  };
  return labels[key] ? `${labels[key]} (${key})` : key;
}

function scopeValueLabel(value: string) {
  const labels: Record<string, string> = {
    calculate: "计算",
    read: "读取",
    write: "写入",
    search: "检索",
    execute: "执行"
  };
  return labels[value] ?? value;
}

function sideEffectLabel(level: string) {
  const labels: Record<string, string> = {
    none: "无副作用",
    external_read: "外部读取",
    internal_write: "内部写入",
    external_write: "外部写入",
    destructive: "破坏性"
  };
  return labels[level] ?? level;
}

function sensitivityLabel(level: string) {
  const labels: Record<string, string> = {
    public: "公开数据",
    internal: "内部数据",
    confidential: "机密数据",
    secret: "敏感密钥"
  };
  return labels[level] ?? level;
}

function networkLabel(access: string) {
  const labels: Record<string, string> = {
    none: "无网络",
    internal_only: "仅内网",
    external: "外网"
  };
  return labels[access] ?? access;
}

function roleIdLabel(roleId: string) {
  const labels: Record<string, string> = {
    platform_admin: "平台管理员",
    office_tools: "办公工具",
    analyst_tools: "分析工具",
    time_tools: "时间查询"
  };
  return labels[roleId] ?? roleId;
}

function groupedToolSections(tools: ToolDefinition[]) {
  const layers = new Map<string, ToolDefinition[]>();
  for (const tool of tools) {
    const layer = tool.layer || "support";
    layers.set(layer, [...(layers.get(layer) ?? []), tool]);
  }
  return Array.from(layers.entries()).map(([layer, rows]) => ({
    layer,
    rows: rows.sort((left, right) =>
      `${left.group_id}.${left.category}.${left.tool_id}`.localeCompare(`${right.group_id}.${right.category}.${right.tool_id}`)
    )
  }));
}

type ToolDisplayGroup = {
  key: string;
  title: string;
  subtitle: string;
  source: ToolDefinition["source"];
  rows: ToolDefinition[];
};

type ActiveToolGroup = {
  layer: string;
  group: ToolDisplayGroup;
};

function groupToolsForDisplay(rows: ToolDefinition[]): ToolDisplayGroup[] {
  const groups = new Map<string, ToolDisplayGroup>();
  for (const tool of rows) {
    const key =
      tool.source === "mcp"
        ? `mcp:${tool.mcp_connection_id ?? tool.group_id}`
        : `${tool.source}:${tool.group_id}`;
    const title =
      tool.source === "mcp"
        ? `MCP：${tool.mcp_connection?.name ?? tool.group_name ?? tool.mcp_connection_id ?? "未命名连接"}`
        : tool.group_name || sourceLabel(tool.source);
    const subtitle =
      tool.source === "mcp"
        ? `连接 ID：${tool.mcp_connection_id ?? "未绑定"}`
        : `${sourceLabel(tool.source)} / ${tool.namespace || "default"}`;
    const group = groups.get(key) ?? {
      key,
      title,
      subtitle,
      source: tool.source,
      rows: []
    };
    group.rows.push(tool);
    groups.set(key, group);
  }
  return Array.from(groups.values()).map((group) => ({
    ...group,
    rows: group.rows.sort((left, right) => toolName(left).localeCompare(toolName(right)))
  }));
}

function schemaPropertySummary(schema: Record<string, unknown>) {
  const properties = schema.properties;
  if (!properties || typeof properties !== "object" || Array.isArray(properties)) return [];
  return Object.entries(properties as Record<string, Record<string, unknown>>)
    .slice(0, 4)
    .map(([name, property]) => ({
      name,
      description:
        typeof property?.description === "string" && property.description
          ? property.description
          : String(property?.type ?? "参数")
    }));
}

function uniqueToolIds(toolIds: string[]) {
  return Array.from(new Set(toolIds));
}

function apiErrorMessage(detail: unknown) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (!item || typeof item !== "object") return String(item);
        const record = item as Record<string, unknown>;
        const loc = Array.isArray(record.loc) ? record.loc.join(".") : "";
        const msg = typeof record.msg === "string" ? record.msg : JSON.stringify(record);
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("；");
  }
  return detail ? JSON.stringify(detail) : "请求失败";
}

async function request<T>(path: string, init?: RequestInit, token?: string | null): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init?.headers
    }
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(apiErrorMessage(data.detail));
  return data as T;
}

function approvalIdOf(approval: ApprovalRequest): string {
  return String(approval.approval_id ?? approval.id ?? "");
}

function formatApprovalValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function headerRowsFromRecord(headers: Record<string, string> | undefined) {
  return Object.entries(headers ?? {}).map(([key, value]) => ({ key, value }));
}

function headersFromRows(rows: Array<{ key?: string; value?: string }> | undefined) {
  return Object.fromEntries(
    (rows ?? [])
      .map((row) => [String(row.key ?? "").trim(), String(row.value ?? "")] as const)
      .filter(([key]) => key)
  );
}

function tokenFromLocation(): string | null {
  const search = new URLSearchParams(window.location.search);
  if (search.get("access_token")) return search.get("access_token");
  const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  return hash.get("access_token");
}

function cleanTokenFromLocation() {
  if (!window.location.search.includes("access_token") && !window.location.hash.includes("access_token")) {
    return;
  }
  window.history.replaceState({}, document.title, window.location.pathname);
}

export default function App() {
  const [activeView, setActiveView] = useState<View>("plaza");
  const [authToken, setAuthToken] = useState<string | null>(() =>
    window.localStorage.getItem("agent_platform_token")
  );
  const [authConfig, setAuthConfig] = useState<AuthConfig>({ auth_mode: "local" });
  const [currentUser, setCurrentUser] = useState<UserProfile | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [mcpConnections, setMcpConnections] = useState<MCPConnection[]>([]);
  const [releases, setReleases] = useState<AgentRelease[]>([]);
  const [roles, setRoles] = useState<Role[]>([]);
  const [adminUsers, setAdminUsers] = useState<AdminUser[]>([]);
  const [roleDrafts, setRoleDrafts] = useState<Record<string, string[]>>({});
  const [editingRole, setEditingRole] = useState<Role | null>(null);
  const [editingAgentId, setEditingAgentId] = useState<string | null>(null);
  const [editingMcpConnection, setEditingMcpConnection] = useState<MCPConnection | null>(null);
  const [mcpConnectionEditorOpen, setMcpConnectionEditorOpen] = useState(false);
  const [workflowEditorOpen, setWorkflowEditorOpen] = useState(false);
  const [lastRun, setLastRun] = useState<RunResult | null>(null);
  const [submittingApprovalId, setSubmittingApprovalId] = useState<string | null>(null);
  const [expandedToolLayers, setExpandedToolLayers] = useState<string[]>([]);
  const [toolPickerScale, setToolPickerScale] = useState<"compact" | "standard" | "wide">("standard");
  const [loading, setLoading] = useState(false);
  const [loginForm] = Form.useForm();
  const [agentForm] = Form.useForm();
  const [runForm] = Form.useForm();
  const [roleEditorForm] = Form.useForm();
  const [mcpConnectionForm] = Form.useForm();
  const [workflowForm] = Form.useForm();
  const selectedAgentId = Form.useWatch("agent_id", runForm);
  const debugMode = Form.useWatch("mode", runForm) ?? "draft";
  const selectedReleaseId = Form.useWatch("release_id", runForm);
  const rolePermissionRows = Form.useWatch("permissions", roleEditorForm);
  const selectedAgentToolIds = Form.useWatch("tools", agentForm) ?? [];
  const agentVisibility = Form.useWatch("visibility", agentForm) ?? "private";
  const mcpTransport =
    Form.useWatch("transport", mcpConnectionForm) ?? "streamable_http";
  const currentUserId = currentUser?.user_id ?? DEFAULT_USER;
  const isOaAuth = authConfig.auth_mode === "oa";

  const agentOptions = useMemo(
    () => agents.map((agent) => ({ value: agent.agent_id, label: agent.name })),
    [agents]
  );
  const activeTools = useMemo(() => tools.filter((tool) => tool.status === "active"), [tools]);
  const toolSections = useMemo(() => groupedToolSections(tools), [tools]);
  const activeToolSections = useMemo(() => groupedToolSections(activeTools), [activeTools]);
  const activeToolGroups = useMemo<ActiveToolGroup[]>(
    () =>
      activeToolSections.flatMap((section) =>
        groupToolsForDisplay(section.rows).map((group) => ({
          layer: section.layer,
          group
        }))
      ),
    [activeToolSections]
  );
  const toolById = useMemo(
    () => new Map(tools.map((tool) => [tool.tool_id, tool])),
    [tools]
  );
  const toolIds = useMemo(() => activeTools.map((tool) => tool.tool_id), [activeTools]);
  const activeToolIdSet = useMemo(() => new Set(toolIds), [toolIds]);
  const toolOptions = useMemo(
    () =>
      activeToolGroups.map(({ layer, group }) => ({
          label: `${layerLabel(layer)} / ${group.title}`,
          options: group.rows.map((tool) => ({
            label: `${toolName(tool)} (${tool.tool_id})`,
            value: tool.tool_id
          }))
        })),
    [activeToolGroups]
  );
  const selectedAgent = agents.find((agent) => agent.agent_id === selectedAgentId);
  const selectedRelease = releases.find((release) => release.release_id === selectedReleaseId);
  const selectedActiveTools = selectedAgent?.tools.filter((tool) => activeToolIdSet.has(tool)) ?? [];
  const selectedReleaseActiveTools =
    selectedRelease?.config_json.tools
      ?.map((tool) => tool.tool_id)
      .filter((toolId) => activeToolIdSet.has(toolId)) ?? [];

  async function refresh(token = authToken) {
    const [nextAgents, nextRuns, nextTools] = await Promise.all([
      request<Agent[]>("/agents", undefined, token),
      request<Run[]>("/runs", undefined, token),
      request<ToolDefinition[]>("/tools", undefined, token)
    ]);
    setAgents(nextAgents);
    setRuns(nextRuns);
    setTools(nextTools);
  }

  async function loadCurrentUser(token: string) {
    const user = await request<UserProfile>("/auth/me", undefined, token);
    setCurrentUser(user);
    runForm.setFieldValue("actor_id", user.user_id);
    agentForm.setFieldValue("owner_user_id", user.user_id);
    return user;
  }

  async function loadAccessControl(token = authToken) {
    if (!token || !currentUser?.is_platform_admin) return;
    const [nextRoles, nextUsers, nextConnections] = await Promise.all([
      request<Role[]>("/admin/roles", undefined, token),
      request<AdminUser[]>("/admin/users", undefined, token),
      request<MCPConnection[]>("/admin/mcp-connections", undefined, token)
    ]);
    setRoles(nextRoles);
    setAdminUsers(nextUsers);
    setMcpConnections(nextConnections);
    setRoleDrafts(Object.fromEntries(nextUsers.map((user) => [user.user_id, user.roles])));
  }

  async function loadReleases(agentId: string) {
    const nextReleases = await request<AgentRelease[]>(`/agents/${agentId}/releases`, undefined, authToken);
    setReleases(nextReleases);
    const published = nextReleases.find((release) => release.status === "published");
    runForm.setFieldValue("release_id", published?.release_id);
  }

  async function login(userId: string) {
    const result = await request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ user_id: userId })
    });
    window.localStorage.setItem("agent_platform_token", result.access_token);
    setAuthToken(result.access_token);
    setCurrentUser(result.user);
    runForm.setFieldValue("actor_id", result.user.user_id);
    agentForm.setFieldValue("owner_user_id", result.user.user_id);
    await refresh(result.access_token);
  }

  async function loginWithToken(token: string) {
    const nextToken = token.trim();
    if (!nextToken) throw new Error("请输入 OA Token");
    window.localStorage.setItem("agent_platform_token", nextToken);
    setAuthToken(nextToken);
    const user = await loadCurrentUser(nextToken);
    setCurrentUser(user);
    await refresh(nextToken);
  }

  async function logout() {
    if (authToken) {
      await request("/auth/logout", { method: "POST" }, authToken).catch(() => undefined);
    }
    window.localStorage.removeItem("agent_platform_token");
    setAuthToken(null);
    setCurrentUser(null);
    setActiveView("plaza");
  }

  async function act(label: string, action: () => Promise<void>) {
    setLoading(true);
    try {
      await action();
      toast.success(label);
      await refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "请求失败");
    } finally {
      setLoading(false);
    }
  }

  async function copyText(value: string, label: string) {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
      } else {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
      }
      toast.success(`${label}已复制`);
    } catch {
      toast.error("复制失败，请手动复制");
    }
  }

  function copyAgentId(agent: Agent) {
    return copyText(agent.agent_id, "Agent ID");
  }

  function AgentIdCopy({ agent, variant = "dark" }: { agent: Agent; variant?: "dark" | "light" }) {
    return (
      <span
        className={`agent-id-copy ${variant === "light" ? "light" : ""}`}
        onClick={(event) => {
          event.stopPropagation();
          copyAgentId(agent);
        }}
        title="复制 Agent ID"
      >
        <CopyOutlined />
        <span>
          <small>Agent ID</small>
          <code>{agent.agent_id}</code>
        </span>
      </span>
    );
  }

  function notifyRunnerResult(result: RunResult, successLabel: string) {
    const status = result.status ?? "succeeded";
    if (status === "pending_approval") {
      toast.warning("等待人工审批");
    } else if (status === "failed") {
      toast.error(result.answer || "运行失败");
    } else {
      toast.success(successLabel);
    }
  }

  async function decideApproval(approval: ApprovalRequest, decision: "approved" | "rejected") {
    const approvalId = approvalIdOf(approval);
    if (!approvalId) {
      toast.error("审批请求缺少 approval_id");
      return;
    }
    setLoading(true);
    setSubmittingApprovalId(approvalId);
    try {
      const result = await request<RunResult>(
        `/gateway/approvals/${encodeURIComponent(approvalId)}/decide`,
        {
          method: "POST",
          body: JSON.stringify({
            actor_id: currentUserId,
            decision
          })
        },
        authToken
      );
      setLastRun(result);
      notifyRunnerResult(result, decision === "approved" ? "已同意审批" : "已拒绝审批");
      await refresh();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "请求失败");
    } finally {
      setSubmittingApprovalId(null);
      setLoading(false);
    }
  }

  function selectAgent(agent: Agent, view: View = "debug") {
    runForm.setFieldValue("agent_id", agent.agent_id);
    runForm.setFieldValue("actor_id", currentUser?.user_id ?? agent.owner_user_id);
    setActiveView(view);
  }

  function canManageAgent(agent?: Agent) {
    return Boolean(
      agent && currentUser &&
      (currentUser.is_platform_admin || agent.owner_user_id === currentUser.user_id)
    );
  }

  function startCreateAgent() {
    setEditingAgentId(null);
    agentForm.resetFields();
    agentForm.setFieldsValue({
      owner_user_id: currentUserId,
      icon: "robot",
      visibility: "private",
      members: [],
      model: DEFAULT_MODEL,
      tools: toolIds,
      system_prompt: "你是内部工具助手。优先根据工具结果回答，中文简短。"
    });
    setActiveView("setup");
  }

  async function openAgentEditor(agent: Agent) {
    setLoading(true);
    try {
      const detail = await request<AgentDetail>(`/agents/${agent.agent_id}`, undefined, authToken);
      setEditingAgentId(agent.agent_id);
      agentForm.setFieldsValue({
        owner_user_id: detail.owner_user_id,
        name: detail.name,
        icon: detail.icon,
        visibility: detail.visibility,
        members: detail.members,
        model: detail.model ?? DEFAULT_MODEL,
        tools: detail.tools,
        system_prompt: detail.system_prompt
      });
      runForm.setFieldValue("agent_id", agent.agent_id);
      setActiveView("setup");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "配置加载失败");
    } finally {
      setLoading(false);
    }
  }

  function toolLabel(toolId: string) {
    return toolName(toolById.get(toolId), toolId);
  }

  function renderRuntimeTags(tool: ToolDefinition) {
    return (
      <Space wrap size={4}>
        <Tag color={tool.read_only ? "green" : "volcano"}>
          {tool.read_only ? "只读" : "可写"}
        </Tag>
        <Tag color={tool.idempotent ? "green" : "gold"}>
          {tool.idempotent ? "可重试" : "不可重试"}
        </Tag>
        <Tag color={tool.parallel_safe ? "green" : "default"}>
          {tool.parallel_safe ? "可并发" : "需串行"}
        </Tag>
        {tool.requires_approval && <Tag color="red">需审批</Tag>}
        <Tag>{sideEffectLabel(tool.side_effect_level)}</Tag>
      </Space>
    );
  }

  function renderDataTags(tool: ToolDefinition) {
    return (
      <Space wrap size={4}>
        <Tag>{sensitivityLabel(tool.data_sensitivity)}</Tag>
        <Tag>{networkLabel(tool.network_access)}</Tag>
        <Tag>{tool.timeout_ms} ms</Tag>
      </Space>
    );
  }

  function renderParameterTags(tool: ToolDefinition) {
    const params = schemaPropertySummary(tool.input_schema);
    if (!params.length) return <Typography.Text type="secondary">无显式参数</Typography.Text>;
    return (
      <Space wrap size={4}>
        {params.map((param) => (
          <Tag key={`${tool.tool_id}-${param.name}`}>
            {param.name}：{param.description}
          </Tag>
        ))}
      </Space>
    );
  }

  function renderScope(scope: Record<string, string[]> | undefined, toolId: string) {
    const normalized = scope ?? {};
    const entries = Object.entries(normalized).filter(([, values]) => values.length > 0);
    if (!entries.length) return <Typography.Text type="secondary">无额外范围</Typography.Text>;
    const tool = toolById.get(toolId);
    return (
      <Space wrap size={4}>
        {entries.flatMap(([scopeKey, values]) =>
          values.map((value) => (
            <Tag color="cyan" key={`${toolId}-${scopeKey}-${value}`}>
              {tool?.scope_descriptions?.[scopeKey] ?? scopeKeyLabel(scopeKey)}：{scopeValueLabel(value)}
            </Tag>
          ))
        )}
      </Space>
    );
  }

  function renderToolTag(toolId: string, activeAware = false) {
    const active = activeToolIdSet.has(toolId);
    return (
      <Tag color={!activeAware || active ? "cyan" : "default"} key={toolId}>
        {toolIcon(toolId)} {toolLabel(toolId)}
        {activeAware && !active ? "（已停用）" : ""}
      </Tag>
    );
  }

  function renderApprovalCard(approval: ApprovalRequest) {
    const approvalId = approvalIdOf(approval);
    const rawToolId = String(approval.tool_id ?? approval.tool_name ?? approval.name ?? "");
    const detail =
      formatApprovalValue(approval.input) ||
      formatApprovalValue(approval.arguments) ||
      formatApprovalValue(approval.payload);
    const submittingThisApproval = submittingApprovalId === approvalId;
    return (
      <div className="approval-card">
        <div className="approval-title">
          <Space wrap>
            <ExclamationCircleOutlined />
            <Typography.Text strong>等待人工审批</Typography.Text>
            {approvalId && <Tag color="gold">审批单：{approvalId}</Tag>}
            {rawToolId && (toolById.has(rawToolId) ? renderToolTag(rawToolId) : <Tag>{rawToolId}</Tag>)}
          </Space>
        </div>
        {(approval.summary || approval.reason) && (
          <Typography.Paragraph className="approval-reason">
            {String(approval.summary ?? approval.reason)}
          </Typography.Paragraph>
        )}
        {detail && <pre className="approval-payload">{detail}</pre>}
        <Space wrap>
          <Button
            type="primary"
            icon={<CheckCircleOutlined />}
            loading={submittingThisApproval}
            disabled={Boolean(submittingApprovalId) && !submittingThisApproval}
            onClick={() => decideApproval(approval, "approved")}
          >
            同意执行
          </Button>
          <Button
            danger
            icon={<CloseCircleOutlined />}
            loading={submittingThisApproval}
            disabled={Boolean(submittingApprovalId) && !submittingThisApproval}
            onClick={() => decideApproval(approval, "rejected")}
          >
            拒绝执行
          </Button>
        </Space>
      </div>
    );
  }

  function groupedToolsByIds(toolIdsToGroup: string[]) {
    const knownTools = toolIdsToGroup
      .map((toolId) => toolById.get(toolId))
      .filter(Boolean) as ToolDefinition[];
    return groupedToolSections(knownTools).flatMap((section) =>
      groupToolsForDisplay(section.rows).map((group) => ({
        layer: section.layer,
        group
      }))
    );
  }

  function normalizePermissionScope(
    toolId: string | undefined,
    scope: Record<string, string[]> | undefined
  ) {
    const tool = toolId ? toolById.get(toolId) : undefined;
    const schema = tool?.scope_schema ?? {};
    const clean: Record<string, string[]> = {};
    for (const [key, values] of Object.entries(scope ?? {})) {
      const allowedValues = schema[key] ?? [];
      if (!allowedValues.length || !Array.isArray(values)) continue;
      const selected = values.filter((value) => allowedValues.includes(value));
      if (selected.length) clean[key] = selected;
    }
    return clean;
  }

  function setAgentToolGroup(toolIdsToChange: string[], checked: boolean) {
    const current = new Set<string>(agentForm.getFieldValue("tools") ?? []);
    for (const toolId of toolIdsToChange) {
      if (checked) current.add(toolId);
      else current.delete(toolId);
    }
    agentForm.setFieldValue(
      "tools",
      uniqueToolIds(toolIds.filter((toolId) => current.has(toolId)))
    );
  }

  function rolePermissionToolIds() {
    return new Set(
      ((roleEditorForm.getFieldValue("permissions") ?? []) as Array<{ tool_id?: string }>)
        .map((permission) => permission.tool_id)
        .filter(Boolean) as string[]
    );
  }

  function addRolePermissionTools(toolIdsToAdd: string[]) {
    const current = ((roleEditorForm.getFieldValue("permissions") ?? []) as Array<{
      tool_id?: string;
      scope?: Record<string, string[]>;
    }>).filter((permission) => permission.tool_id);
    const existing = new Set(current.map((permission) => permission.tool_id as string));
    const additions = toolIdsToAdd
      .filter((toolId) => !existing.has(toolId))
      .map((toolId) => ({
        tool_id: toolId,
        scope: Object.fromEntries(
          Object.entries(toolById.get(toolId)?.scope_schema ?? {}).map(([key, values]) => [
            key,
            values
          ])
        )
      }));
    roleEditorForm.setFieldValue("permissions", [...current, ...additions]);
  }

  function removeRolePermissionTools(toolIdsToRemove: string[]) {
    const removeSet = new Set(toolIdsToRemove);
    const current = (roleEditorForm.getFieldValue("permissions") ?? []) as Array<{
      tool_id?: string;
      scope?: Record<string, string[]>;
    }>;
    roleEditorForm.setFieldValue(
      "permissions",
      current.filter((permission) => !permission.tool_id || !removeSet.has(permission.tool_id))
    );
  }

  function ToolCapabilityCard({ tool }: { tool: ToolDefinition }) {
    return (
      <label className="tool-pick-card">
        <Checkbox value={tool.tool_id} />
        <div>
          <div className="tool-pick-title">
            {toolIcon(tool.tool_id)}
            <strong>{toolName(tool)}</strong>
            <Tag color={sourceColor(tool.source)}>{sourceLabel(tool.source)}</Tag>
          </div>
          <Typography.Paragraph className="tool-card-desc" type="secondary" ellipsis={{ rows: 2 }}>
            {toolDescription(tool)}
          </Typography.Paragraph>
          <Space wrap size={4}>
            <Tag>{categoryLabel(tool.category)}</Tag>
            {tool.runner_name && <Tag>远端：{tool.runner_name}</Tag>}
            <Typography.Text type="secondary">{tool.tool_id}</Typography.Text>
          </Space>
          <div className="runtime-strip">{renderRuntimeTags(tool)}</div>
          <div className="runtime-strip">{renderDataTags(tool)}</div>
          <div className="runtime-strip">{renderParameterTags(tool)}</div>
        </div>
      </label>
    );
  }

  useEffect(() => {
    request<AuthConfig>("/auth/config")
      .then(setAuthConfig)
      .catch(() => setAuthConfig({ auth_mode: "local" }));
    const urlToken = tokenFromLocation();
    const nextToken = urlToken ?? authToken;
    if (urlToken) {
      window.localStorage.setItem("agent_platform_token", urlToken);
      setAuthToken(urlToken);
      cleanTokenFromLocation();
    }
    refresh(nextToken).catch(() => undefined);
    if (nextToken) {
      loadCurrentUser(nextToken).catch(() => {
        window.localStorage.removeItem("agent_platform_token");
        setAuthToken(null);
        setCurrentUser(null);
      });
    }
  }, []);

  useEffect(() => {
    const currentTools = agentForm.getFieldValue("tools");
    if (!editingAgentId && (!currentTools || currentTools.length === 0) && toolIds.length > 0) {
      agentForm.setFieldValue("tools", toolIds);
    }
  }, [toolIds, agentForm, editingAgentId]);

  useEffect(() => {
    if (!selectedAgentId) {
      setReleases([]);
      return;
    }
    loadReleases(selectedAgentId).catch(() => setReleases([]));
  }, [selectedAgentId]);

  useEffect(() => {
    if ((activeView === "access" || activeView === "tools") && currentUser?.is_platform_admin) {
      loadAccessControl().catch((error) =>
        toast.error(error instanceof Error ? error.message : "治理数据加载失败")
      );
    }
  }, [activeView, currentUser?.is_platform_admin, authToken]);

  const navItems: Array<{ key: View; label: string; icon: React.ReactNode }> = [
    { key: "plaza", label: "Agent 广场", icon: <AppstoreOutlined /> },
    { key: "debug", label: "调试运行", icon: <PlayCircleOutlined /> },
    { key: "setup", label: "配置中心", icon: <SettingOutlined /> },
    ...(currentUser?.is_platform_admin
      ? [
          { key: "tools" as View, label: "工具治理", icon: <ThunderboltOutlined /> },
          { key: "access" as View, label: "权限治理", icon: <SafetyCertificateOutlined /> }
        ]
      : []),
    { key: "profile", label: "用户中心", icon: <UserOutlined /> },
    { key: "agents", label: "Agent 列表", icon: <RobotOutlined /> },
    { key: "runs", label: "Runs 日志", icon: <HistoryOutlined /> }
  ];

  const titles: Record<View, string> = {
    plaza: "Agent 广场",
    debug: selectedAgent ? `调试 ${selectedAgent.name}` : "调试运行",
    setup: "配置中心",
    tools: "工具治理",
    access: "权限治理",
    profile: "用户中心",
    agents: "Agent 列表",
    runs: "Runs 日志"
  };

  const subtitles: Record<View, string> = {
    plaza: "每个 Agent 都有自己的办公桌，点击卡片进入调试。",
    debug: "选择 Agent、工具和消息，直接调用真实 LLM。",
    setup: "初始化本地工具、创建 Agent。",
    tools: "查看工具 manifest，并开启或关闭运行注入。",
    access: "仅平台管理员可管理角色工具范围和用户角色。",
    profile: "查看当前登录用户、角色和工具权限。",
    agents: "查看当前数据库中的 Agent 和授权工具。",
    runs: "查看最近运行记录和模型回复。"
  };

  function AgentDeskCard({ agent, large = false }: { agent: Agent; large?: boolean }) {
    const groupedTools = groupedToolsByIds(agent.tools);
    const unknownToolIds = agent.tools.filter((toolId) => !toolById.has(toolId));
    return (
      <button
        className={`agent-card ${large ? "large" : ""} ${
          agent.agent_id === selectedAgentId ? "active" : ""
        }`}
        onClick={() => selectAgent(agent)}
        type="button"
      >
        <div className="desk-scene">
          <span className="desk-lamp" />
          <span className="desk-monitor">
            <DesktopOutlined />
          </span>
          <span className="desk-agent">{agentIcon(agent)}</span>
          <span className="desk-chair" />
          <span className="desk-board" />
        </div>
        <div className="agent-card-body">
          <div className="agent-title">
            <strong>{agent.name}</strong>
            <Space size={4}>
              {visibilityTag(agent)}
              <Tag color={agent.enabled ? "green" : "default"}>{agent.enabled ? "启用" : "停用"}</Tag>
            </Space>
          </div>
          <span className="agent-owner">创建人：{agent.owner_user_id}</span>
          <AgentIdCopy agent={agent} />
          {agent.visibility === "private" && agent.members.length > 0 && (
            <div className="agent-members">
              {agent.members.slice(0, 3).map((member) => (
                <Tag icon={<UserOutlined />} key={member}>{member}</Tag>
              ))}
            </div>
          )}
          <div className="agent-tools">
            {groupedTools.length || unknownToolIds.length ? (
              <>
                {groupedTools.map(({ layer, group }) => (
                  <span className="agent-tool-group" key={`${agent.agent_id}-${layer}-${group.key}`}>
                    <ThunderboltOutlined />
                    <span>
                      <strong>{group.title}</strong>
                      <small>{layerLabel(layer)} / {group.rows.length} 个工具</small>
                    </span>
                  </span>
                ))}
                {unknownToolIds.length > 0 && (
                  <span className="agent-tool-group muted-group">
                    <ThunderboltOutlined />
                    <span>
                      <strong>未同步工具</strong>
                      <small>{unknownToolIds.length} 个历史引用</small>
                    </span>
                  </span>
                )}
              </>
            ) : (
              <Typography.Text type="secondary">未配置工具</Typography.Text>
            )}
          </div>
        </div>
      </button>
    );
  }

  function renderLoginGate() {
    return (
      <section className="login-page">
        <div className="login-panel">
          <div className="brand-row">
            <div className="brand-mark">AP</div>
            <div>
              <Typography.Title level={3}>Agent Platform</Typography.Title>
              <Typography.Text className="muted">登录后进入控制台</Typography.Text>
            </div>
          </div>
          <Form
            form={loginForm}
            layout="vertical"
            initialValues={isOaAuth ? {} : { user_id: DEFAULT_USER }}
            onFinish={(values) =>
              act("登录成功", async () => {
                if (isOaAuth) {
                  await loginWithToken(values.oa_token);
                  return;
                }
                await login(values.user_id);
              })
            }
          >
            {isOaAuth ? (
              <>
                <Alert
                  type="info"
                  showIcon
                  message="当前使用 OA 统一登录"
                  description="平台只保存 OA 用户映射和 Agent 权限，身份有效性由 OA Token 校验。"
                />
                <Form.Item name="oa_token" label="OA Token" rules={[{ required: true }]}>
                  <Input.Password prefix={<SafetyCertificateOutlined />} placeholder="粘贴 OA Bearer Token" />
                </Form.Item>
                <Space wrap>
                  <Button type="primary" htmlType="submit" icon={<LoginOutlined />} loading={loading}>
                    使用 Token 进入
                  </Button>
                  {authConfig.oa_login_url && (
                    <Button
                      icon={<GlobalOutlined />}
                      onClick={() => {
                        window.location.href = authConfig.oa_login_url ?? "";
                      }}
                    >
                      前往 OA 登录
                    </Button>
                  )}
                </Space>
              </>
            ) : (
              <>
                <Form.Item name="user_id" label="用户 ID" rules={[{ required: true }]}>
                  <Input prefix={<UserOutlined />} placeholder="u_console" />
                </Form.Item>
                <Space wrap>
                  <Button type="primary" htmlType="submit" icon={<LoginOutlined />} loading={loading}>
                    登录
                  </Button>
                  <Button
                    icon={<SafetyCertificateOutlined />}
                    loading={loading}
                    onClick={() =>
                      act("已初始化并登录", async () => {
                        await request("/admin/seed-roles", { method: "POST" });
                        const result = await request<LoginResponse>("/auth/login", {
                          method: "POST",
                          body: JSON.stringify({ user_id: DEFAULT_USER })
                        });
                        await request(`/admin/users/${DEFAULT_USER}/roles`, {
                          method: "PUT",
                          body: JSON.stringify({
                            actor_id: DEFAULT_USER,
                            roles: ["platform_admin", "office_tools"]
                          })
                        }, result.access_token);
                        window.localStorage.setItem("agent_platform_token", result.access_token);
                        setAuthToken(result.access_token);
                        await loadCurrentUser(result.access_token);
                        await refresh(result.access_token);
                      })
                    }
                  >
                    初始化并登录
                  </Button>
                </Space>
              </>
            )}
          </Form>
        </div>
      </section>
    );
  }

  function renderPlaza() {
    return (
      <section className="plaza-page">
        <div className="hero-panel">
          <div>
            <Typography.Title level={3}>Agent 办公桌广场</Typography.Title>
            <Typography.Text>点击任意办公桌，右侧工作台会切到对应 Agent 的调试上下文。</Typography.Text>
          </div>
          <Space>
            <Tag color="green">{agents.length} 个 Agent</Tag>
            <Tag color="cyan">{toolIds.length} 个可用工具</Tag>
          </Space>
        </div>
        <div className="plaza-grid">
          {agents.length ? (
            agents.map((agent) => <AgentDeskCard agent={agent} key={agent.agent_id} large />)
          ) : (
            <div className="plaza-empty">先到配置中心创建 Agent。</div>
          )}
        </div>
      </section>
    );
  }

  function renderDebug() {
    async function publishSelectedAgent() {
      if (!selectedAgent) throw new Error("请先选择 Agent");
      const release = await request<AgentRelease>(
        `/agents/${selectedAgent.agent_id}/releases`,
        {
          method: "POST",
          body: JSON.stringify({ actor_id: currentUserId })
        },
        authToken
      );
      await loadReleases(selectedAgent.agent_id);
      runForm.setFieldsValue({ mode: "release", release_id: release.release_id });
    }

    async function submitAgentRun(values: {
      agent_id: string;
      release_id?: string;
      mode?: string;
      actor_id: string;
      message: string;
      runner_backend?: string;
    }) {
      setLoading(true);
      setSubmittingApprovalId(null);
      try {
        const isReleaseMode = values.mode === "release";
        const path = isReleaseMode
          ? `/agents/${values.agent_id}/releases/${values.release_id}/runs`
          : `/agents/${values.agent_id}/runs`;
        const toolIdsForRun = isReleaseMode ? selectedReleaseActiveTools : selectedActiveTools;
        const result = await request<RunResult>(
          path,
          {
            method: "POST",
            body: JSON.stringify({
              actor_id: values.actor_id,
              channel: "web_console",
              message: values.message,
              runner_backend: values.runner_backend,
              tool_ids: toolIdsForRun
            })
          },
          authToken
        );
        setLastRun(result);
        notifyRunnerResult(result, "运行完成");
        await refresh();
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "请求失败");
      } finally {
        setLoading(false);
      }
    }

    const runStatus = lastRun?.status ?? "succeeded";
    const pendingApproval =
      runStatus === "pending_approval" ? lastRun?.approval_requests?.[0] : undefined;
    const resultAlertType =
      runStatus === "pending_approval" ? "warning" : runStatus === "failed" ? "error" : "success";

    return (
      <section className="grid two">
        <div className="panel">
          <Typography.Title level={4}>运行 Agent</Typography.Title>
          <Form
            form={runForm}
            layout="vertical"
            initialValues={{
              actor_id: currentUserId,
              mode: "draft",
              runner_backend: DEFAULT_RUNNER_BACKEND
            }}
            onFinish={submitAgentRun}
          >
            <Form.Item name="agent_id" label="Agent" rules={[{ required: true }]}>
              <Select options={agentOptions} placeholder="选择 Agent" />
            </Form.Item>
            {selectedAgent && <AgentIdCopy agent={selectedAgent} variant="light" />}
            <div className="form-grid">
              <Form.Item name="mode" label="调试模式">
                <Select
                  options={[
                    { label: "草稿配置", value: "draft" },
                    { label: "发布版本", value: "release" }
                  ]}
                />
              </Form.Item>
              {debugMode === "release" && (
                <Form.Item name="release_id" label="发布版本" rules={[{ required: true }]}>
                  <Select
                    placeholder="选择发布版本"
                    options={releases.map((release) => ({
                      label: `v${release.version} · ${statusLabel(release.status)}`,
                      value: release.release_id
                    }))}
                  />
                </Form.Item>
              )}
              <Form.Item name="runner_backend" label="Runner">
                <Select
                  options={[
                    { label: "内部 Debug Runner", value: "debug" },
                    { label: "真实 Gateway Runner", value: "gateway" }
                  ]}
                />
              </Form.Item>
            </div>
            <Form.Item name="actor_id" label="执行用户" rules={[{ required: true }]}>
              <Input />
            </Form.Item>
            <div className="configured-tools">
              {(debugMode === "release"
                ? selectedRelease?.config_json.tools?.map((tool) => tool.tool_id) ?? []
                : selectedAgent?.tools ?? []
              ).map((tool) => (
                  renderToolTag(tool, true)
                ))}
              {!selectedAgent && <Tag>先选择 Agent</Tag>}
              {debugMode === "release" && selectedAgent && !selectedRelease && <Tag>暂无发布版本</Tag>}
            </div>
            <Form.Item name="message" label="消息" rules={[{ required: true }]}>
              <Input.TextArea rows={5} placeholder="1 + 2 * 3 / 帮我统计这句话 / 现在几点" />
            </Form.Item>
            <Space wrap>
              <Button type="primary" htmlType="submit" icon={<PlayCircleOutlined />} loading={loading}>
                运行
              </Button>
              {canManageAgent(selectedAgent) && (
                <Button
                  icon={<PlusOutlined />}
                  loading={loading}
                  onClick={() => act("发布版本已生成", publishSelectedAgent)}
                >
                  发布当前 Agent
                </Button>
              )}
              {canManageAgent(selectedAgent) && selectedAgent && (
                <Button
                  icon={<EditOutlined />}
                  loading={loading}
                  onClick={() => openAgentEditor(selectedAgent)}
                >
                  编辑配置
                </Button>
              )}
            </Space>
          </Form>
        </div>

        <div className="panel result">
          <Typography.Title level={4}>结果</Typography.Title>
          {lastRun ? (
            <Space direction="vertical" size="middle" className="wide">
              <Alert
                type={resultAlertType}
                showIcon
                message={statusLabel(runStatus)}
                description={lastRun.answer}
              />
              {pendingApproval && renderApprovalCard(pendingApproval)}
              <Table
                rowKey="tool_id"
                size="small"
                pagination={false}
                dataSource={lastRun.tool_calls ?? []}
                columns={[
                  {
                    title: "工具",
                    dataIndex: "tool_id",
                    render: (toolId: string) => renderToolTag(toolId)
                  },
                  {
                    title: "状态",
                    dataIndex: "status",
                    render: (status: string) => (
                      <Tag color={status === "succeeded" ? "green" : "red"}>
                        {statusLabel(status)}
                      </Tag>
                    )
                  },
                  { title: "结果", dataIndex: "result", ellipsis: true }
                ]}
              />
            </Space>
          ) : (
            <div className="empty">暂无调试结果</div>
          )}
        </div>
      </section>
    );
  }

  function renderSetup() {
    const memberOptions = Array.from(
      new Set([currentUserId, ...adminUsers.map((user) => user.user_id)])
    )
      .filter((userId) => userId !== currentUserId)
      .map((userId) => ({ label: userId, value: userId }));

    return (
      <section className="grid two">
        <div className="panel">
          <Typography.Title level={4}>初始化权限</Typography.Title>
          <Space direction="vertical" className="wide">
            <Alert type="info" showIcon message={`当前用户 ${currentUserId}，已加载 ${toolIds.length} 个可用工具`} />
            <Button
              icon={<SafetyCertificateOutlined />}
              loading={loading}
              onClick={() =>
                act("权限已初始化", async () => {
                  await request("/admin/seed-roles", { method: "POST" }, authToken);
                  await request(`/users/${currentUserId}`, {
                    method: "PUT",
                    body: JSON.stringify({ status: "active" })
                  }, authToken);
                  if (currentUser?.is_platform_admin) {
                    await request(`/admin/users/${currentUserId}/roles`, {
                      method: "PUT",
                      body: JSON.stringify({
                        actor_id: currentUserId,
                        roles: ["platform_admin", "office_tools"]
                      })
                    }, authToken);
                    await loadCurrentUser(authToken ?? "");
                  }
                })
              }
            >
              初始化
            </Button>
          </Space>
        </div>

        <div className="panel span">
          <Typography.Title level={4}>
            {editingAgentId ? "更新 Agent 配置" : "创建 Agent"}
          </Typography.Title>
          <Form
            form={agentForm}
            layout="vertical"
            initialValues={{
              owner_user_id: DEFAULT_USER,
              icon: "robot",
              visibility: "private",
              members: [],
              model: DEFAULT_MODEL,
              tools: toolIds,
              system_prompt: "你是内部工具助手。优先根据工具结果回答，中文简短。"
            }}
            onFinish={(values) =>
              act(editingAgentId ? "Agent 配置已更新" : "Agent 已创建", async () => {
                const members = values.visibility === "private" ? values.members ?? [] : [];
                if (editingAgentId) {
                  await request<AgentDetail>(`/agents/${editingAgentId}`, {
                    method: "PUT",
                    body: JSON.stringify({
                      actor_id: currentUserId,
                      name: values.name,
                      icon: values.icon,
                      visibility: values.visibility,
                      members,
                      model: values.model,
                      tools: values.tools,
                      channels: ["web_console"],
                      system_prompt: values.system_prompt
                    })
                  }, authToken);
                  runForm.setFieldValue("agent_id", editingAgentId);
                  setEditingAgentId(null);
                  setActiveView("debug");
                  return;
                }
                const result = await request<{ agent_id: string }>("/agents", {
                  method: "POST",
                  body: JSON.stringify({ ...values, members })
                }, authToken);
                runForm.setFieldValue("agent_id", result.agent_id);
                setActiveView("plaza");
              })
            }
          >
            <div className="form-grid">
              <Form.Item name="name" label="名称" rules={[{ required: true }]}>
                <Input placeholder="office-agent" />
              </Form.Item>
              <Form.Item name="icon" label="图标">
                <Select options={ICON_OPTIONS} />
              </Form.Item>
              <Form.Item name="owner_user_id" label="创建者" rules={[{ required: true }]}>
                <Input disabled={Boolean(editingAgentId)} />
              </Form.Item>
              <Form.Item name="visibility" label="类型">
                <Select
                  options={[
                    { label: "私有自建 Agent", value: "private" },
                    { label: "公开发布 Agent", value: "public" }
                  ]}
                />
              </Form.Item>
              {agentVisibility === "private" && (
                <Form.Item name="members" label="可用成员">
                  <Select
                    mode="tags"
                    allowClear
                    options={memberOptions}
                    placeholder="输入用户 ID，例如 u_analyst"
                  />
                </Form.Item>
              )}
              <Form.Item name="model" label="模型">
                <Input />
              </Form.Item>
            </div>
            <Form.Item name="tools" label="工具能力">
              <Checkbox.Group className={`tool-picker tool-picker-${toolPickerScale}`}>
                <div className="tool-picker-toolbar">
                  <Space wrap>
                    <Button
                      size="small"
                      onClick={() => setExpandedToolLayers(activeToolSections.map((section) => section.layer))}
                    >
                      全部展开
                    </Button>
                    <Button size="small" onClick={() => setExpandedToolLayers([])}>
                      全部收起
                    </Button>
                  </Space>
                  <Segmented
                    size="small"
                    value={toolPickerScale}
                    onChange={(value) => setToolPickerScale(value as "compact" | "standard" | "wide")}
                    options={[
                      { label: "紧凑", value: "compact" },
                      { label: "标准", value: "standard" },
                      { label: "宽松", value: "wide" }
                    ]}
                  />
                </div>
                <Collapse
                  activeKey={expandedToolLayers}
                  className="tool-picker-collapse"
                  onChange={(keys) => setExpandedToolLayers(Array.isArray(keys) ? keys.map(String) : [String(keys)])}
                  items={activeToolSections.map((section) => {
                    const toolGroups = groupToolsForDisplay(section.rows);
                    return {
                      key: section.layer,
                      label: (
                        <Space wrap>
                          <Tag color={layerColor(section.layer)}>{layerLabel(section.layer)}</Tag>
                          <Typography.Text type="secondary">
                            {section.rows.length} 个可用工具
                          </Typography.Text>
                        </Space>
                      ),
                      children: (
                        <Collapse
                          className="tool-group-collapse"
                          items={toolGroups.map((group) => {
                            const selectedCount = group.rows.filter((tool) =>
                              selectedAgentToolIds.includes(tool.tool_id)
                            ).length;
                            const groupToolIds = group.rows.map((tool) => tool.tool_id);
                            return {
                              key: group.key,
                              label: (
                                <Space direction="vertical" size={0}>
                                  <Space wrap>
                                    <Tag color={sourceColor(group.source)}>{group.title}</Tag>
                                    <Tag>
                                      已选 {selectedCount}/{group.rows.length}
                                    </Tag>
                                  </Space>
                                  <Typography.Text type="secondary">{group.subtitle}</Typography.Text>
                                </Space>
                              ),
                              extra: (
                                <Space wrap size={4} onClick={(event) => event.stopPropagation()}>
                                  <Button
                                    size="small"
                                    onClick={() => setAgentToolGroup(groupToolIds, true)}
                                  >
                                    全选本组
                                  </Button>
                                  <Button
                                    size="small"
                                    onClick={() => setAgentToolGroup(groupToolIds, false)}
                                  >
                                    清空
                                  </Button>
                                </Space>
                              ),
                              children: (
                                <div className="tool-card-list">
                                  {group.rows.map((tool) => (
                                    <ToolCapabilityCard tool={tool} key={tool.tool_id} />
                                  ))}
                                </div>
                              )
                            };
                          })}
                        />
                      )
                    };
                  })}
                />
              </Checkbox.Group>
            </Form.Item>
            <Form.Item name="system_prompt" label="System Prompt">
              <Input.TextArea rows={4} />
            </Form.Item>
            <Space wrap>
              <Button
                type="primary"
                htmlType="submit"
                icon={editingAgentId ? <EditOutlined /> : <PlusOutlined />}
                loading={loading}
              >
                {editingAgentId ? "保存配置" : "创建"}
              </Button>
              {editingAgentId && (
                <Button onClick={startCreateAgent}>取消编辑</Button>
              )}
            </Space>
          </Form>
        </div>
      </section>
    );
  }

  function renderAgents() {
    return (
      <div className="panel">
        <Table
          rowKey="agent_id"
          dataSource={agents}
          pagination={false}
          columns={[
            {
              title: "图标",
              dataIndex: "icon",
              render: (_icon: string, agent: Agent) => agentIcon(agent)
            },
            { title: "名称", dataIndex: "name" },
            { title: "创建者", dataIndex: "owner_user_id" },
            {
              title: "类型",
              dataIndex: "visibility",
              render: (_: string, agent: Agent) => visibilityTag(agent)
            },
            {
              title: "成员",
              dataIndex: "members",
              render: (members: string[], agent: Agent) =>
                agent.visibility === "public" ? (
                  <Tag color="cyan">所有 active 用户</Tag>
                ) : members.length ? (
                  members.map((member) => <Tag key={member}>{member}</Tag>)
                ) : (
                  <Typography.Text type="secondary">仅创建者</Typography.Text>
                )
            },
            {
              title: "状态",
              dataIndex: "enabled",
              render: (enabled: boolean) => (
                <Tag color={enabled ? "green" : "default"}>{enabled ? "启用" : "停用"}</Tag>
              )
            },
            {
              title: "工具",
              dataIndex: "tools",
              render: (tools: string[]) => tools.map((tool) => renderToolTag(tool))
            },
            {
              title: "操作",
              key: "actions",
	              render: (_: unknown, agent: Agent) => (
	                <Space wrap>
	                  <Button
	                    size="small"
	                    icon={<CopyOutlined />}
	                    onClick={() => copyAgentId(agent)}
	                  >
	                    复制 ID
	                  </Button>
	                  {canManageAgent(agent) && (
	                    <Button
                      size="small"
                      icon={<EditOutlined />}
                      onClick={() => openAgentEditor(agent)}
                    >
                      编辑
                    </Button>
                  )}
                  {currentUser?.is_platform_admin && (
                    <Button
                      size="small"
                      danger={agent.enabled}
                      icon={<PoweroffOutlined />}
                      loading={loading}
                      onClick={() =>
                        act(agent.enabled ? "Agent 已停用" : "Agent 已启用", async () => {
                          await request(`/admin/agents/${agent.agent_id}/status`, {
                            method: "PUT",
                            body: JSON.stringify({
                              actor_id: currentUserId,
                              enabled: !agent.enabled
                            })
                          }, authToken);
                        })
                      }
                    >
                      {agent.enabled ? "停用" : "启用"}
                    </Button>
                  )}
                </Space>
              )
            }
          ]}
        />
      </div>
    );
  }

  function renderAccessControl() {
    const roleOptions = roles.map((role) => ({
      label: `${role.name} (${role.role_id})`,
      value: role.role_id
    }));
    const selectedRolePermissionIds = new Set(
      ((rolePermissionRows ?? []) as Array<{ tool_id?: string }>)
        .map((permission) => permission.tool_id)
        .filter(Boolean) as string[]
    );

    function openRoleEditor(role?: Role) {
      setEditingRole(role ?? null);
      roleEditorForm.setFieldsValue({
        role_id: role?.role_id ?? "",
        name: role?.name ?? "",
        description: role?.description ?? "",
        status: role?.status ?? "active",
        permissions: role?.permissions.map((permission) => ({
          tool_id: permission.tool_id,
          scope: permission.scope
        })) ?? []
      });
    }

    async function saveRoles(user: AdminUser) {
      const nextRoles = roleDrafts[user.user_id] ?? [];
      setLoading(true);
      try {
        await request(
          `/admin/users/${user.user_id}/roles`,
          {
            method: "PUT",
            body: JSON.stringify({ actor_id: currentUserId, roles: nextRoles })
          },
          authToken
        );
        await loadAccessControl();
        if (user.user_id === currentUserId && authToken) await loadCurrentUser(authToken);
        toast.success(`${user.user_id} 的角色已更新`);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "角色保存失败");
      } finally {
        setLoading(false);
      }
    }

    async function saveCustomRole(values: {
      role_id: string;
      name: string;
      description: string;
      status: string;
      permissions?: Array<{ tool_id?: string; scope?: Record<string, string[]> }>;
    }) {
      const permissions = (values.permissions ?? [])
        .filter((permission) => permission.tool_id)
        .map((permission) => ({
          tool_id: permission.tool_id as string,
          scope: normalizePermissionScope(permission.tool_id, permission.scope)
        }));
      const path = editingRole ? `/admin/roles/${editingRole.role_id}` : "/admin/roles";
      const method = editingRole ? "PUT" : "POST";

      setLoading(true);
      try {
        await request(
          path,
          {
            method,
            body: JSON.stringify({
              actor_id: currentUserId,
              role_id: values.role_id,
              name: values.name,
              description: values.description,
              status: values.status,
              permissions
            })
          },
          authToken
        );
        await loadAccessControl();
        toast.success(editingRole ? "自定义角色已更新" : "自定义角色已创建");
        openRoleEditor();
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "角色保存失败");
      } finally {
        setLoading(false);
      }
    }

    return (
      <section className="access-page">
        <div className="access-intro">
          <SafetyCertificateOutlined />
          <div>
            <Typography.Title level={4}>角色授权控制面</Typography.Title>
            <Typography.Text>角色决定可配置和可执行的工具范围；多角色 scope 按 allow-list 合并。</Typography.Text>
          </div>
          <Tag color="orange">管理员专用</Tag>
        </div>

        <div className="access-grid">
          <div className="panel">
            <Space className="panel-heading" align="center">
              <Typography.Title level={4}>角色库</Typography.Title>
              <Button size="small" icon={<PlusOutlined />} onClick={() => openRoleEditor()}>
                新建角色
              </Button>
            </Space>
            <Table
              rowKey="role_id"
              size="small"
              pagination={false}
              dataSource={roles}
              columns={[
                {
                  title: "角色",
                  dataIndex: "name",
                  render: (_: string, role: Role) => (
                    <Space direction="vertical" size={0}>
                      <strong>{role.name}</strong>
                      <Typography.Text type="secondary">{role.role_id}</Typography.Text>
                      {role.description && <Typography.Text type="secondary">{role.description}</Typography.Text>}
                    </Space>
                  )
                },
                {
                  title: "工具授权范围",
                  dataIndex: "permissions",
                  render: (permissions: Role["permissions"]) =>
                    permissions.length ? (
                      <Space wrap>
                        {permissions.map((permission) => (
                          <div className="scope-summary" key={permission.tool_id}>
                            <strong>{toolLabel(permission.tool_id)}</strong>
                            {renderScope(permission.scope, permission.tool_id)}
                          </div>
                        ))}
                      </Space>
                    ) : (
                      <Typography.Text type="secondary">平台治理权限，无工具执行权限</Typography.Text>
                    )
                },
                {
                  title: "状态",
                  key: "status",
                  render: (_: unknown, role: Role) => (
                    <Space wrap size={4}>
                      <Tag color={role.status === "active" ? "green" : "default"}>
                        {statusLabel(role.status)}
                      </Tag>
                      {role.is_system && <Tag color="gold">系统</Tag>}
                    </Space>
                  )
                },
                {
                  title: "操作",
                  key: "action",
                  render: (_: unknown, role: Role) =>
                    role.is_system ? (
                      <Typography.Text type="secondary">受保护</Typography.Text>
                    ) : (
                      <Button size="small" icon={<EditOutlined />} onClick={() => openRoleEditor(role)}>
                        编辑
                      </Button>
                    )
                }
              ]}
            />
          </div>

          <div className="panel">
            <Typography.Title level={4}>{editingRole ? "编辑自定义角色" : "新建自定义角色"}</Typography.Title>
            <Form
              form={roleEditorForm}
              layout="vertical"
              initialValues={{ status: "active", permissions: [] }}
              onFinish={saveCustomRole}
            >
              <div className="form-grid">
                <Form.Item
                  name="role_id"
                  label="角色 ID"
                  rules={[
                    { required: true },
                    {
                      pattern: /^[\w\u4e00-\u9fa5][\w\u4e00-\u9fa5.-]*$/,
                      message: "支持中英文、数字、点、下划线和横线，不支持空格或斜杠"
                    }
                  ]}
                  extra="角色 ID 创建后不可修改，可直接使用中文，例如 高德mcp。"
                >
                  <Input disabled={Boolean(editingRole)} placeholder="finance_readonly" />
                </Form.Item>
                <Form.Item name="name" label="显示名称" rules={[{ required: true }]}> 
                  <Input placeholder="财务只读" />
                </Form.Item>
                <Form.Item name="status" label="状态">
                  <Select options={[{ label: "启用", value: "active" }, { label: "停用", value: "disabled" }]} />
                </Form.Item>
              </div>
              <Form.Item name="description" label="说明">
                <Input.TextArea rows={2} placeholder="说明该角色的业务边界" />
              </Form.Item>
              <Typography.Title level={5}>工具与授权范围</Typography.Title>
              <div className="permission-bulk-picker">
                {activeToolGroups.map(({ layer, group }) => {
                  const groupToolIds = group.rows.map((tool) => tool.tool_id);
                  const selectedCount = groupToolIds.filter((toolId) =>
                    selectedRolePermissionIds.has(toolId)
                  ).length;
                  return (
                    <div className="permission-bulk-group" key={`role-${layer}-${group.key}`}>
                      <div>
                        <Space wrap>
                          <Tag color={layerColor(layer)}>{layerLabel(layer)}</Tag>
                          <Tag color={sourceColor(group.source)}>{group.title}</Tag>
                          <Typography.Text type="secondary">{group.subtitle}</Typography.Text>
                        </Space>
                        <Typography.Text type="secondary">
                          已授权 {selectedCount}/{group.rows.length} 个工具
                        </Typography.Text>
                      </div>
                      <Space wrap>
                        <Button
                          size="small"
                          onClick={() => addRolePermissionTools(groupToolIds)}
                        >
                          加入本组
                        </Button>
                        <Button
                          size="small"
                          onClick={() => removeRolePermissionTools(groupToolIds)}
                        >
                          移除本组
                        </Button>
                      </Space>
                    </div>
                  );
                })}
              </div>
              <Form.List name="permissions">
                {(fields, { add, remove }) => (
                  <Space direction="vertical" className="wide" size="small">
                    {fields.map((field) => {
                      const toolId = rolePermissionRows?.[field.name]?.tool_id;
                      const selectedTool = tools.find((tool) => tool.tool_id === toolId);
                      const scopeEntries = Object.entries(selectedTool?.scope_schema ?? {});
                      return (
                        <div className="permission-row" key={field.key}>
                          <Form.Item {...field} name={[field.name, "tool_id"]} rules={[{ required: true }]}>
                            <Select
                              options={toolOptions}
                              optionFilterProp="label"
                              placeholder="选择工具"
                              showSearch
                            />
                          </Form.Item>
                          <Space direction="vertical" className="permission-scopes" size={4}>
                            {selectedTool && (
                              <div className="permission-tool-meta">
                                <Space wrap>
                                  <strong>{toolName(selectedTool)}</strong>
                                  <Tag color={sourceColor(selectedTool.source)}>{sourceLabel(selectedTool.source)}</Tag>
                                  {selectedTool.mcp_connection_id && (
                                    <Tag>MCP：{selectedTool.mcp_connection?.name ?? selectedTool.mcp_connection_id}</Tag>
                                  )}
                                </Space>
                                <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }}>
                                  {toolDescription(selectedTool)}
                                </Typography.Paragraph>
                                <div>{renderParameterTags(selectedTool)}</div>
                              </div>
                            )}
                            {scopeEntries.length ? (
                              scopeEntries.map(([scopeKey, values]) => (
                                <Form.Item
                                  key={scopeKey}
                                  label={
                                    <Space direction="vertical" size={0}>
                                      <span>{scopeKeyLabel(scopeKey)}</span>
                                      {selectedTool?.scope_descriptions?.[scopeKey] && (
                                        <Typography.Text type="secondary">
                                          {selectedTool.scope_descriptions[scopeKey]}
                                        </Typography.Text>
                                      )}
                                    </Space>
                                  }
                                  name={[field.name, "scope", scopeKey]}
                                  rules={[{ required: true }]}
                                >
                                  <Checkbox.Group
                                    options={values.map((value) => ({
                                      label: scopeValueLabel(value),
                                      value
                                    }))}
                                  />
                                </Form.Item>
                              ))
                            ) : (
                              <Typography.Text type="secondary">
                                该工具未声明可裁剪 scope；保存后表示角色拥有此工具的工具级权限。
                              </Typography.Text>
                            )}
                          </Space>
                          <Button type="text" danger icon={<MinusCircleOutlined />} onClick={() => remove(field.name)} />
                        </div>
                      );
                    })}
                    <Button type="dashed" icon={<PlusOutlined />} onClick={() => add({ scope: {} })}>
                      添加工具权限
                    </Button>
                  </Space>
                )}
              </Form.List>
              <Space>
                <Button type="primary" htmlType="submit" icon={editingRole ? <EditOutlined /> : <PlusOutlined />} loading={loading}>
                  {editingRole ? "保存角色" : "创建角色"}
                </Button>
                {editingRole && <Button onClick={() => openRoleEditor()}>取消编辑</Button>}
              </Space>
            </Form>
          </div>
        </div>

        <div className="panel access-users">
          <Typography.Title level={4}>用户角色分配</Typography.Title>
            <Table
              rowKey="user_id"
              size="small"
              pagination={{ pageSize: 8 }}
              dataSource={adminUsers}
              columns={[
                { title: "用户", dataIndex: "user_id" },
                {
                  title: "状态",
                  dataIndex: "status",
                  render: (status: string) => (
                    <Tag color={status === "active" ? "green" : "default"}>{statusLabel(status)}</Tag>
                  )
                },
                {
                  title: "角色",
                  dataIndex: "roles",
                  width: "48%",
                  render: (_: string[], user: AdminUser) => (
                    <Select
                      mode="multiple"
                      allowClear
                      className="role-select"
                      options={roleOptions}
                      value={roleDrafts[user.user_id] ?? []}
                      onChange={(values) =>
                        setRoleDrafts((drafts) => ({ ...drafts, [user.user_id]: values }))
                      }
                    />
                  )
                },
                {
                  title: "操作",
                  key: "action",
                  render: (_: unknown, user: AdminUser) => (
                    <Button
                      size="small"
                      type="primary"
                      loading={loading}
                      onClick={() => saveRoles(user)}
                    >
                      保存
                    </Button>
                  )
                }
              ]}
            />
        </div>
      </section>
    );
  }

  function renderToolGovernance() {
    function openMcpConnectionEditor(connection?: MCPConnection) {
      setEditingMcpConnection(connection ?? null);
      mcpConnectionForm.setFieldsValue(
        connection
          ? {
              connection_id: connection.connection_id,
              name: connection.name,
              status: connection.status,
              transport: connection.transport,
              url: connection.config.url,
              command: connection.config.command,
              args_text: (connection.config.args ?? []).join("\n"),
              cwd: connection.config.cwd,
              env_vars_text: (connection.config.env_vars ?? []).join(","),
              bearer_env: connection.config.bearer_env,
              header_rows: headerRowsFromRecord(connection.config.headers),
              connect_timeout_ms: connection.config.connect_timeout_ms ?? 10000,
              call_timeout_ms: connection.config.call_timeout_ms ?? 30000,
              idle_ttl_seconds: connection.config.idle_ttl_seconds ?? 900,
              discovery_ttl_seconds: connection.config.discovery_ttl_seconds ?? 300
            }
          : {
              connection_id: "",
              name: "",
              status: "active",
              transport: "streamable_http",
              header_rows: [],
              connect_timeout_ms: 10000,
              call_timeout_ms: 30000,
              idle_ttl_seconds: 900,
              discovery_ttl_seconds: 300
            }
      );
      setMcpConnectionEditorOpen(true);
    }

    async function saveMcpConnection() {
      const values = await mcpConnectionForm.validateFields();
      const connectionId = String(values.connection_id).trim();
      const splitValues = (value?: string) =>
        String(value ?? "")
          .split(/[,\n]/)
          .map((item) => item.trim())
          .filter(Boolean);
      setLoading(true);
      let connectionSaved = false;
      try {
        await request<MCPConnection>(
          `/admin/mcp-connections/${connectionId}`,
          {
            method: "PUT",
            body: JSON.stringify({
              name: values.name,
              status: values.status,
              transport: values.transport,
              url: values.transport === "streamable_http" ? values.url : null,
              command: values.transport === "stdio" ? values.command : null,
              args: values.transport === "stdio" ? splitValues(values.args_text) : [],
              cwd: values.transport === "stdio" ? values.cwd || null : null,
              env_vars:
                values.transport === "stdio" ? splitValues(values.env_vars_text) : [],
              bearer_env:
                values.transport === "streamable_http" ? values.bearer_env || null : null,
              headers:
                values.transport === "streamable_http"
                  ? headersFromRows(values.header_rows)
                  : {},
              connect_timeout_ms: values.connect_timeout_ms,
              call_timeout_ms: values.call_timeout_ms,
              idle_ttl_seconds: values.idle_ttl_seconds,
              discovery_ttl_seconds: values.discovery_ttl_seconds
            })
          },
          authToken
        );
        connectionSaved = true;
        let discovery: MCPDiscoveryResult | null = null;
        if (values.status === "active") {
          discovery = await request<MCPDiscoveryResult>(
            `/admin/mcp-connections/${connectionId}/discover`,
            { method: "POST" },
            authToken
          );
        }
        await Promise.all([loadAccessControl(), refresh()]);
        setMcpConnectionEditorOpen(false);
        toast.success(
          discovery
            ? `连接已保存，发现 ${discovery.discovered_count} 个工具`
            : `MCP 连接 ${connectionId} 已保存`
        );
      } catch (error) {
        if (connectionSaved) {
          await Promise.all([loadAccessControl(), refresh()]);
          setMcpConnectionEditorOpen(false);
          toast.warning(
            `连接已保存，但工具发现失败：${
              error instanceof Error ? error.message : "请检查 MCP 服务"
            }`
          );
        } else {
          toast.error(error instanceof Error ? error.message : "MCP 连接保存失败");
        }
      } finally {
        setLoading(false);
      }
    }

    function upsertMcpHeader(key: string, value: string) {
      const rows = (mcpConnectionForm.getFieldValue("header_rows") ?? []) as Array<{
        key?: string;
        value?: string;
      }>;
      const index = rows.findIndex((row) => String(row.key ?? "").toLowerCase() === key.toLowerCase());
      const nextRows = [...rows];
      if (index >= 0) {
        nextRows[index] = { ...nextRows[index], key, value };
      } else {
        nextRows.push({ key, value });
      }
      mcpConnectionForm.setFieldValue("header_rows", nextRows);
    }

    async function discoverMcpTools(connection: MCPConnection) {
      setLoading(true);
      try {
        const result = await request<MCPDiscoveryResult>(
          `/admin/mcp-connections/${connection.connection_id}/discover`,
          { method: "POST" },
          authToken
        );
        await Promise.all([loadAccessControl(), refresh()]);
        toast.success(
          `发现 ${result.discovered_count} 个工具${
            result.disabled_tool_ids.length
              ? `，停用 ${result.disabled_tool_ids.length} 个已移除工具`
              : ""
          }`
        );
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "MCP 工具发现失败");
      } finally {
        setLoading(false);
      }
    }

    async function updateToolStatus(tool: ToolDefinition, status: "active" | "disabled") {
      setLoading(true);
      try {
        await request(
          `/admin/tools/${tool.tool_id}/status`,
          {
            method: "PUT",
            body: JSON.stringify({ actor_id: currentUserId, status })
          },
          authToken
        );
        await refresh();
        toast.success(`${tool.tool_id} 已${status === "active" ? "开启" : "关闭"}`);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "工具状态更新失败");
      } finally {
        setLoading(false);
      }
    }

    function openWorkflowEditor() {
      workflowForm.resetFields();
      workflowForm.setFieldsValue({
        version: "dev",
        requires_approval: false,
        side_effect_level: "internal_write",
        timeout_ms: 30000,
        input_schema_text: '{\n  "type": "object",\n  "properties": {}\n}'
      });
      setWorkflowEditorOpen(true);
    }

    async function saveWorkflowCapability() {
      const values = await workflowForm.validateFields();
      let inputSchema: Record<string, unknown> = {};
      try {
        inputSchema = JSON.parse(values.input_schema_text || "{}") as Record<string, unknown>;
      } catch {
        toast.error("Input Schema 必须是合法 JSON");
        return;
      }
      setLoading(true);
      try {
        await request(
          `/admin/workflows/${values.workflow_id}`,
          {
            method: "PUT",
            body: JSON.stringify({
              name: values.name,
              description: values.description || "",
              status: "active",
              mcp_connection_id: values.mcp_connection_id,
              runner_name: values.runner_name,
              version: values.version || "dev",
              requires_approval: Boolean(values.requires_approval),
              side_effect_level: values.side_effect_level || "internal_write",
              data_sensitivity: "internal",
              timeout_ms: values.timeout_ms || 30000,
              input_schema: inputSchema,
              scope_schema: {},
              scope_descriptions: {}
            })
          },
          authToken
        );
        await refresh();
        setWorkflowEditorOpen(false);
        toast.success(`工作流能力 ${values.workflow_id} 已注册`);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "工作流能力注册失败");
      } finally {
        setLoading(false);
      }
    }

    return (
      <section className="tool-governance">
        <div className="access-intro">
          <ThunderboltOutlined />
          <div>
            <Typography.Title level={4}>工具运行开关</Typography.Title>
            <Typography.Text>Manifest 来自 Runner 或 seed；控制台只负责查看和启停。</Typography.Text>
          </div>
          <Space wrap>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={openWorkflowEditor}
              disabled={!mcpConnections.length}
            >
              注册工作流能力
            </Button>
            <Tag color="orange">管理员专用</Tag>
          </Space>
        </div>

        <div className="panel">
          <div className="tool-layer-heading">
            <Space wrap>
              <GlobalOutlined />
              <Typography.Title level={5}>MCP 连接</Typography.Title>
              <Typography.Text type="secondary">
                一个连接可以挂载多个工具；配置版本变化后 Runner 会替换旧会话。
              </Typography.Text>
            </Space>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => openMcpConnectionEditor()}
            >
              新增连接
            </Button>
          </div>
          <Table
            rowKey="connection_id"
            dataSource={mcpConnections}
            pagination={false}
            columns={[
              {
                title: "连接",
                key: "connection",
                render: (_: unknown, connection: MCPConnection) => (
                  <Space direction="vertical" size={0}>
                    <strong>{connection.name}</strong>
                    <Typography.Text type="secondary">
                      {connection.connection_id}
                    </Typography.Text>
                  </Space>
                )
              },
              {
                title: "Transport",
                dataIndex: "transport",
                render: (transport: MCPConnection["transport"]) => (
                  <Tag color={transport === "streamable_http" ? "geekblue" : "cyan"}>
                    {transport === "streamable_http" ? "Streamable HTTP" : "stdio"}
                  </Tag>
                )
              },
              {
                title: "目标",
                key: "target",
                render: (_: unknown, connection: MCPConnection) => (
                  <Typography.Text code>
                    {connection.config.url ?? connection.config.command ?? "未配置"}
                  </Typography.Text>
                )
              },
              {
                title: "缓存",
                key: "cache",
                render: (_: unknown, connection: MCPConnection) => (
                  <Space direction="vertical" size={0}>
                    <Typography.Text>
                      空闲 {connection.config.idle_ttl_seconds ?? 900}s
                    </Typography.Text>
                    <Typography.Text type="secondary">
                      发现 {connection.config.discovery_ttl_seconds ?? 300}s
                    </Typography.Text>
                  </Space>
                )
              },
              {
                title: "工具",
                key: "tools",
                render: (_: unknown, connection: MCPConnection) => {
                  const count = tools.filter(
                    (tool) => tool.mcp_connection_id === connection.connection_id
                  ).length;
                  return <Tag color={count ? "cyan" : "default"}>{count} 个</Tag>;
                }
              },
              {
                title: "版本",
                key: "version",
                render: (_: unknown, connection: MCPConnection) => (
                  <Space direction="vertical" size={0}>
                    <strong>v{connection.config_version}</strong>
                    <Typography.Text type="secondary" ellipsis>
                      {connection.config_hash}
                    </Typography.Text>
                  </Space>
                )
              },
              {
                title: "状态",
                dataIndex: "status",
                render: (status: string) => (
                  <Tag color={status === "active" ? "green" : "default"}>
                    {statusLabel(status)}
                  </Tag>
                )
              },
              {
                title: "操作",
                key: "action",
                render: (_: unknown, connection: MCPConnection) => (
                  <Space wrap>
                    <Button
                      icon={<ThunderboltOutlined />}
                      disabled={connection.status !== "active"}
                      loading={loading}
                      onClick={() => discoverMcpTools(connection)}
                    >
                      重新发现
                    </Button>
                    <Button
                      icon={<EditOutlined />}
                      onClick={() => openMcpConnectionEditor(connection)}
                    >
                      编辑
                    </Button>
                  </Space>
                )
              }
            ]}
          />
        </div>

        <div className="tool-layer-stack">
          {toolSections.map((section) => (
            <div className="panel tool-layer-panel" key={section.layer}>
              <div className="tool-layer-heading">
                <Space wrap>
                  <Tag color={layerColor(section.layer)}>{layerLabel(section.layer)}</Tag>
                  <Typography.Text type="secondary">{section.rows.length} 个工具</Typography.Text>
                </Space>
              </div>
              <Collapse
                className="tool-governance-collapse"
                defaultActiveKey={groupToolsForDisplay(section.rows).slice(0, 1).map((group) => group.key)}
                items={groupToolsForDisplay(section.rows).map((group) => ({
                  key: group.key,
                  label: (
                    <Space wrap>
                      <Tag color={sourceColor(group.source)}>{group.title}</Tag>
                      <Typography.Text type="secondary">{group.subtitle}</Typography.Text>
                      <Tag>{group.rows.length} 个</Tag>
                    </Space>
                  ),
                  children: (
                    <Table
                      rowKey="tool_id"
                      dataSource={group.rows}
                      pagination={{ pageSize: 6 }}
                      expandable={{
                        expandedRowRender: (tool) => (
                          <div className="tool-detail-grid">
                            <div>
                              <Typography.Text type="secondary">中文说明</Typography.Text>
                              <strong>{toolDescription(tool)}</strong>
                            </div>
                            <div>
                              <Typography.Text type="secondary">层级 / 分组</Typography.Text>
                              <strong>{layerLabel(tool.layer)} / {tool.group_name} / {categoryLabel(tool.category)}</strong>
                            </div>
                            <div>
                              <Typography.Text type="secondary">命名空间</Typography.Text>
                              <strong>{tool.namespace}</strong>
                            </div>
                            <div>
                              <Typography.Text type="secondary">Runner</Typography.Text>
                              <strong>
                                {tool.mcp_connection_id
                                  ? `MCP：${tool.mcp_connection?.name ?? tool.mcp_connection_id}`
                                  : tool.runner_tool_id ?? "未绑定"}
                              </strong>
                            </div>
                            <div>
                              <Typography.Text type="secondary">远端工具名</Typography.Text>
                              <strong>{tool.runner_name ?? "无"}</strong>
                            </div>
                            <div>
                              <Typography.Text type="secondary">参数摘要</Typography.Text>
                              {renderParameterTags(tool)}
                            </div>
                            <div>
                              <Typography.Text type="secondary">Scope 定义</Typography.Text>
                              <Typography.Text code>{JSON.stringify(tool.scope_schema)}</Typography.Text>
                            </div>
                            <div>
                              <Typography.Text type="secondary">版本 / Hash</Typography.Text>
                              <Typography.Text code>{tool.version} / {tool.manifest_hash || "none"}</Typography.Text>
                            </div>
                          </div>
                        )
                      }}
                      columns={[
                        {
                          title: "工具",
                          dataIndex: "name",
                          render: (_: string, tool: ToolDefinition) => (
                            <Space direction="vertical" size={0}>
                              <Space wrap>
                                {toolIcon(tool.tool_id)}
                                <strong>{toolName(tool)}</strong>
                                <Typography.Text type="secondary">{tool.tool_id}</Typography.Text>
                              </Space>
                              <Typography.Text type="secondary" ellipsis>
                                {toolDescription(tool)}
                              </Typography.Text>
                            </Space>
                          )
                        },
                        {
                          title: "能力分类",
                          key: "layer",
                          render: (_: unknown, tool: ToolDefinition) => (
                            <Space wrap size={4}>
                              <Tag color={layerColor(tool.layer)}>{layerLabel(tool.layer)}</Tag>
                              <Tag>{categoryLabel(tool.category)}</Tag>
                            </Space>
                          )
                        },
                        {
                          title: "来源",
                          key: "source",
                          render: (_: unknown, tool: ToolDefinition) => (
                            <Space wrap size={4}>
                              <Tag color={sourceColor(tool.source)}>{sourceLabel(tool.source)}</Tag>
                              {tool.runner_name && <Tag>远端：{tool.runner_name}</Tag>}
                            </Space>
                          )
                        },
                        {
                          title: "执行属性",
                          key: "runtime",
                          render: (_: unknown, tool: ToolDefinition) => renderRuntimeTags(tool)
                        },
                        {
                          title: "状态",
                          dataIndex: "status",
                          render: (status: string) => (
                            <Tag color={status === "active" ? "green" : "default"}>{statusLabel(status)}</Tag>
                          )
                        },
                        {
                          title: "操作",
                          key: "action",
                          render: (_: unknown, tool: ToolDefinition) => (
                            <Button
                              danger={tool.status === "active"}
                              loading={loading}
                              onClick={() => updateToolStatus(tool, tool.status === "active" ? "disabled" : "active")}
                            >
                              {tool.status === "active" ? "关闭" : "开启"}
                            </Button>
                          )
                        }
                      ]}
                    />
                  )
                }))}
              />
            </div>
          ))}
        </div>

        <Modal
          title="注册工作流能力"
          open={workflowEditorOpen}
          confirmLoading={loading}
          onOk={saveWorkflowCapability}
          onCancel={() => setWorkflowEditorOpen(false)}
          okText="注册"
          cancelText="取消"
          width={640}
        >
          <Alert
            type="info"
            showIcon
            message="Workflow 作为受治理 Capability 暴露"
            description="工作流执行复用已登记的 MCP 连接；Release 和运行时权限仍决定最终可调用范围。"
            style={{ marginBottom: 16 }}
          />
          <Form form={workflowForm} layout="vertical">
            <Form.Item
              name="workflow_id"
              label="Workflow ID"
              rules={[
                { required: true },
                { pattern: /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$/, message: "仅支持字母、数字、点、下划线和横线" }
              ]}
            >
              <Input placeholder="expense.submit" />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input placeholder="提交报销审批" />
            </Form.Item>
            <Form.Item name="description" label="描述">
              <Input.TextArea rows={2} placeholder="说明该工作流的业务边界和预期结果" />
            </Form.Item>
            <Space wrap size="large">
              <Form.Item name="mcp_connection_id" label="MCP 连接" rules={[{ required: true }]}>
                <Select
                  style={{ minWidth: 220 }}
                  options={mcpConnections
                    .filter((connection) => connection.status === "active")
                    .map((connection) => ({ value: connection.connection_id, label: connection.name }))}
                />
              </Form.Item>
              <Form.Item name="runner_name" label="远端 Workflow Tool" rules={[{ required: true }]}>
                <Input placeholder="submit_expense" />
              </Form.Item>
            </Space>
            <Space wrap size="large">
              <Form.Item name="version" label="版本">
                <Input placeholder="v1" />
              </Form.Item>
              <Form.Item name="timeout_ms" label="超时（ms）">
                <InputNumber min={100} max={300000} />
              </Form.Item>
              <Form.Item name="requires_approval" valuePropName="checked" label="治理">
                <Checkbox>调用前需要审批</Checkbox>
              </Form.Item>
            </Space>
            <Form.Item name="side_effect_level" label="副作用等级">
              <Select
                options={[
                  { label: "仅读取外部数据", value: "external_read" },
                  { label: "内部写入", value: "internal_write" },
                  { label: "外部写入", value: "external_write" },
                  { label: "破坏性操作", value: "destructive" }
                ]}
              />
            </Form.Item>
            <Form.Item name="input_schema_text" label="Input Schema (JSON)" rules={[{ required: true }]}>
              <Input.TextArea rows={7} />
            </Form.Item>
          </Form>
        </Modal>

        <Modal
          title={editingMcpConnection ? "编辑 MCP 连接" : "新增 MCP 连接"}
          open={mcpConnectionEditorOpen}
          confirmLoading={loading}
          onOk={saveMcpConnection}
          onCancel={() => setMcpConnectionEditorOpen(false)}
          okText="保存"
          cancelText="取消"
          width={680}
        >
          <Form form={mcpConnectionForm} layout="vertical">
            <Form.Item
              name="connection_id"
              label="连接 ID"
              rules={[
                { required: true },
                { pattern: /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}$/, message: "仅支持字母、数字、点、下划线和横线" }
              ]}
            >
              <Input disabled={Boolean(editingMcpConnection)} placeholder="search.public" />
            </Form.Item>
            <Form.Item name="name" label="名称" rules={[{ required: true }]}>
              <Input placeholder="公共搜索 MCP" />
            </Form.Item>
            <Space wrap size="large">
              <Form.Item name="status" label="状态">
                <Segmented
                  options={[
                    { label: "启用", value: "active" },
                    { label: "停用", value: "disabled" }
                  ]}
                />
              </Form.Item>
              <Form.Item name="transport" label="Transport">
                <Segmented
                  options={[
                    { label: "Streamable HTTP", value: "streamable_http" },
                    { label: "stdio", value: "stdio" }
                  ]}
                />
              </Form.Item>
            </Space>
            {mcpTransport === "streamable_http" ? (
              <>
                <Form.Item name="url" label="MCP URL" rules={[{ required: true, type: "url" }]}>
                  <Input placeholder="https://mcp.example.com/mcp" />
                </Form.Item>
                <Form.Item name="bearer_env" label="Bearer 环境变量（兼容旧方式）">
                  <Input placeholder="可留空；新连接建议直接在 Header 中配置 Authorization" />
                </Form.Item>
                <div className="mcp-header-editor">
                  <div className="mcp-header-toolbar">
                    <Typography.Text strong>请求 Header</Typography.Text>
                    <Space wrap>
                      <Button
                        size="small"
                        onClick={() => upsertMcpHeader("Authorization", "Bearer ")}
                      >
                        Bearer
                      </Button>
                      <Button size="small" onClick={() => upsertMcpHeader("x-api-key", "")}>
                        x-api-key
                      </Button>
                      <Button size="small" onClick={() => upsertMcpHeader("Accept", "application/json")}>
                        Accept
                      </Button>
                    </Space>
                  </div>
                  <Form.List name="header_rows">
                    {(fields, { add, remove }) => (
                      <div className="mcp-header-list">
                        {fields.map((field) => (
                          <div className="mcp-header-row" key={field.key}>
                            <Form.Item
                              {...field}
                              name={[field.name, "key"]}
                              rules={[{ required: true, message: "Header 名称必填" }]}
                            >
                              <Input placeholder="Header 名称，如 Authorization / x-api-key" />
                            </Form.Item>
                            <Form.Item {...field} name={[field.name, "value"]}>
                              <Input.Password placeholder="Header 值，如 Bearer sk-..." />
                            </Form.Item>
                            <Button
                              danger
                              icon={<MinusCircleOutlined />}
                              onClick={() => remove(field.name)}
                            />
                          </div>
                        ))}
                        <Button icon={<PlusOutlined />} onClick={() => add({ key: "", value: "" })}>
                          添加 Header
                        </Button>
                      </div>
                    )}
                  </Form.List>
                  <Typography.Text type="secondary">
                    Header 会保存到平台数据库，用于工具发现和后续 Runner 连接；请只授予必要权限。
                  </Typography.Text>
                </div>
              </>
            ) : (
              <>
                <Form.Item name="command" label="Command" rules={[{ required: true }]}>
                  <Input placeholder="/usr/local/bin/python" />
                </Form.Item>
                <Form.Item name="args_text" label="Args（每行一个）">
                  <Input.TextArea rows={3} placeholder={"-m\ncustomer_mcp.server"} />
                </Form.Item>
                <Form.Item name="cwd" label="工作目录">
                  <Input placeholder="/opt/mcp/customer" />
                </Form.Item>
                <Form.Item name="env_vars_text" label="透传环境变量">
                  <Input placeholder="API_KEY,DATABASE_URL" />
                </Form.Item>
              </>
            )}
            <Space wrap size="large">
              <Form.Item name="connect_timeout_ms" label="连接超时（ms）">
                <InputNumber min={100} max={300000} />
              </Form.Item>
              <Form.Item name="call_timeout_ms" label="调用超时（ms）">
                <InputNumber min={100} max={300000} />
              </Form.Item>
              <Form.Item name="idle_ttl_seconds" label="空闲连接 TTL（秒）">
                <InputNumber min={1} max={86400} />
              </Form.Item>
              <Form.Item name="discovery_ttl_seconds" label="工具发现 TTL（秒）">
                <InputNumber min={1} max={86400} />
              </Form.Item>
            </Space>
          </Form>
        </Modal>
      </section>
    );
  }

  function renderProfile() {
    if (!currentUser) return null;
    const scopedToolIds = Object.keys(currentUser.role_tool_scopes);
    const groupedScopes = groupedToolsByIds(scopedToolIds);
    const unknownScopedToolIds = scopedToolIds.filter((toolId) => !toolById.has(toolId));

    return (
      <section className="grid two">
        <div className="panel">
          <Typography.Title level={4}>当前用户</Typography.Title>
          <Space direction="vertical" size="middle" className="wide">
            <div className="profile-card">
              <div className="profile-avatar">
                <UserOutlined />
              </div>
              <div>
                <Typography.Title level={4}>{currentUser.user_id}</Typography.Title>
                <Space wrap>
                  <Tag color={currentUser.status === "active" ? "green" : "default"}>
                    {statusLabel(currentUser.status)}
                  </Tag>
                  {currentUser.is_platform_admin && <Tag color="gold">平台管理员</Tag>}
                </Space>
              </div>
            </div>
            <Space wrap>
              <Button icon={<ReloadOutlined />} onClick={() => authToken && loadCurrentUser(authToken)}>
                刷新用户信息
              </Button>
              <Button danger icon={<LogoutOutlined />} onClick={() => act("已退出登录", logout)}>
                退出登录
              </Button>
            </Space>
          </Space>
        </div>

        <div className="panel">
          <Typography.Title level={4}>角色</Typography.Title>
          <Space wrap>
            {currentUser.roles.length ? (
              currentUser.roles.map((role) => (
                <Tag icon={<TeamOutlined />} color={role === "platform_admin" ? "gold" : "cyan"} key={role}>
                  {roleIdLabel(role)}
                </Tag>
              ))
            ) : (
              <Tag>暂无角色</Tag>
            )}
          </Space>
        </div>

        <div className="panel">
          <Typography.Title level={4}>角色工具范围</Typography.Title>
          {groupedScopes.length || unknownScopedToolIds.length ? (
            <Collapse
              className="profile-tool-collapse tool-group-collapse"
              defaultActiveKey={groupedScopes.map(({ group }) => group.key)}
              items={[
                ...groupedScopes.map(({ layer, group }) => ({
                  key: group.key,
                  label: (
                    <Space direction="vertical" size={0}>
                      <Space wrap>
                        <Tag color={layerColor(layer)}>{layerLabel(layer)}</Tag>
                        <Tag color={sourceColor(group.source)}>{group.title}</Tag>
                        <Tag>{group.rows.length} 个授权工具</Tag>
                      </Space>
                      <Typography.Text type="secondary">{group.subtitle}</Typography.Text>
                    </Space>
                  ),
                  children: (
                    <div className="profile-tool-list">
                      {group.rows.map((tool) => (
                        <div className="profile-tool-card" key={`profile-${tool.tool_id}`}>
                          <div>
                            <Space wrap>
                              {toolIcon(tool.tool_id)}
                              <strong>{toolName(tool)}</strong>
                              <Typography.Text type="secondary">{tool.tool_id}</Typography.Text>
                            </Space>
                            <Typography.Paragraph type="secondary" ellipsis={{ rows: 2 }}>
                              {toolDescription(tool)}
                            </Typography.Paragraph>
                          </div>
                          <div>{renderScope(currentUser.role_tool_scopes[tool.tool_id], tool.tool_id)}</div>
                        </div>
                      ))}
                    </div>
                  )
                })),
                ...(unknownScopedToolIds.length
                  ? [
                      {
                        key: "unknown",
                        label: (
                          <Space wrap>
                            <Tag>未同步工具</Tag>
                            <Tag>{unknownScopedToolIds.length} 个授权引用</Tag>
                          </Space>
                        ),
                        children: (
                          <div className="profile-tool-list">
                            {unknownScopedToolIds.map((toolId) => (
                              <div className="profile-tool-card" key={`profile-${toolId}`}>
                                <strong>{toolId}</strong>
                                <div>{renderScope(currentUser.role_tool_scopes[toolId], toolId)}</div>
                              </div>
                            ))}
                          </div>
                        )
                      }
                    ]
                  : [])
              ]}
            />
          ) : (
            <Typography.Text type="secondary">当前用户暂无工具运行权限</Typography.Text>
          )}
        </div>
      </section>
    );
  }

  function renderRuns() {
    return (
      <div className="panel">
          <Table
          rowKey="run_id"
          dataSource={runs}
          pagination={{ pageSize: 8 }}
          columns={[
            {
              title: "状态",
              dataIndex: "status",
              render: (status: string) => (
                <Tag color={status === "succeeded" ? "green" : status === "failed" ? "red" : "blue"}>
                  {statusLabel(status)}
                </Tag>
              )
            },
            { title: "用户", dataIndex: "actor_id" },
            { title: "消息", dataIndex: "message", ellipsis: true },
            { title: "回复", dataIndex: "answer", ellipsis: true },
            { title: "时间", dataIndex: "created_at" }
          ]}
        />
      </div>
    );
  }

  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#2ac8d2",
          borderRadius: 6,
          fontFamily:
            'Avenir Next, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif'
        }
      }}
    >
      {!currentUser ? (
        renderLoginGate()
      ) : (
      <Layout className="shell">
        <Layout.Sider className="side nav-side" width={260}>
          <div className="brand-row">
            <div className="brand-mark">AP</div>
            <div>
              <Typography.Title level={3}>Agent Platform</Typography.Title>
              <Typography.Text className="muted">Control deck</Typography.Text>
            </div>
          </div>

          <div className="nav-list">
            {navItems.map((item) => (
              <button
                className={`nav-item ${item.key === "access" ? "permission-nav" : ""} ${
                  activeView === item.key ? "active" : ""
                }`}
                key={item.key}
                onClick={() => item.key === "setup" ? startCreateAgent() : setActiveView(item.key)}
                type="button"
              >
                {item.icon}
                <span>{item.label}</span>
              </button>
            ))}
          </div>

          <div className="side-stats">
            <div className="metric">
              <span>Agent 数</span>
              <strong>{agents.length}</strong>
            </div>
            <div className="metric">
              <span>运行数</span>
              <strong>{runs.length}</strong>
            </div>
          </div>

          <button className="user-chip" onClick={() => setActiveView("profile")} type="button">
            <UserOutlined />
            <span>{currentUser.user_id}</span>
            {currentUser.is_platform_admin && <Tag color="gold">管理员</Tag>}
          </button>

          <div className="tool-stack">
            {activeToolGroups.map(({ layer, group }) => (
              <div className="tool-row tool-group-row" key={`side-${layer}-${group.key}`}>
                <ThunderboltOutlined />
                <span>
                  <strong>{group.title}</strong>
                  <small>
                    {layerLabel(layer)} / {group.rows.length} 个启用工具
                  </small>
                </span>
              </div>
            ))}
          </div>
        </Layout.Sider>

        <Layout.Content className="content">
          <div className="topbar">
            <div>
              <Typography.Title level={2}>{titles[activeView]}</Typography.Title>
              <Typography.Text type="secondary">{subtitles[activeView]}</Typography.Text>
            </div>
            <Space>
              <Tag color="green">本地工具</Tag>
              <Tag color="cyan">真实 LLM</Tag>
              <Tag icon={<UserOutlined />} color="blue">
                {currentUser.user_id}
              </Tag>
              <Tag icon={<ThunderboltOutlined />} color="gold">
                动效
              </Tag>
              <Button icon={<ReloadOutlined />} onClick={() => refresh()}>
                刷新
              </Button>
            </Space>
          </div>

          {activeView === "plaza" && renderPlaza()}
          {activeView === "debug" && renderDebug()}
          {activeView === "setup" && renderSetup()}
          {activeView === "tools" && currentUser.is_platform_admin && renderToolGovernance()}
          {activeView === "access" && currentUser.is_platform_admin && renderAccessControl()}
          {activeView === "profile" && renderProfile()}
          {activeView === "agents" && renderAgents()}
          {activeView === "runs" && renderRuns()}
        </Layout.Content>
      </Layout>
      )}
    </ConfigProvider>
  );
}
