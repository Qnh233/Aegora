# Central Configuration

Aegora 的开发配置统一位于仓库根目录 `.env`，模板为 `/.env.example`。

- Control Plane 与 Runtime 都从同一个根 `.env` 读取环境变量。
- 各服务目录只保留**非密钥默认参数**，例如 Runtime 的 `config/settings.toml`。
- Agent Prompt、模型选择、Tool/Workflow Capability、MCP Connection 等业务配置属于 Control Plane 数据库和 Agent Release，不写入 `.env`。
- 生产环境优先使用容器环境变量、Kubernetes Secret / ConfigMap 覆盖根模板中的值。
- `ENV_FILE` 可临时指定其他 dotenv 文件；`AEGORA_ROOT` 可在特殊部署布局中显式指定仓库/应用根目录。

配置优先级：

```text
Process Environment
        >
ENV_FILE / Aegora root .env
        >
service non-secret defaults
```

