# Deployment Boundary

Aegora 后续统一从此目录维护部署入口，避免 Control Plane 与 Runtime 各自复制一套基础设施配置。

建议生产拓扑：

```text
Web Console -> Control Plane API -> PostgreSQL
                                  ^
OA / IM / HTTP -> Runtime Fleet --|
                      |
                      +-> MCP / Workflow Services
                      +-> LiteLLM Proxy -> Model Providers
```

Control Plane 与 Runtime 可以独立扩缩容；Runtime 保持业务无状态。MCP Session、HTTP Pool 等仅作为 Pod 本地可丢弃缓存。

