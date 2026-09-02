# Aegora 开发约定

- 保持 Control Plane、Runtime、共享合同与部署层解耦，禁止跨层直接引用实现模块。
- 配置统一从仓库根目录 `.env` / 环境变量读取；子模块仅定义自身配置模型，不维护独立密钥文件。
- PostgreSQL 是发布配置、权限与治理事实源；Runtime 可以缓存，但不能成为业务事实唯一持有者。
- Agent Release 作为不可变能力上限；运行时权限、工具状态和连接状态必须可实时收敛。
- 新业务能力优先通过 MCP 或 Workflow Capability 接入，Runtime Core 不硬编码具体业务。
- 修改功能必须补测试并同步 README / docs 中的架构与边界说明。

