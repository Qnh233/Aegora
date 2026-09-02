# Aegora Config Contract

该目录定义跨服务的**配置约定**，不提供会把 Control Plane 与 Runtime 耦合到一起的运行时实现包。

当前约定：

1. 根 `.env` 是开发环境统一入口；
2. `AEGORA_ROOT` 可覆盖 monorepo root 自动发现；
3. `ENV_FILE` 可覆盖 dotenv 路径；
4. 动态业务配置以 PostgreSQL 中的 Release / Registry 为事实源；
5. 服务自己的非密钥调优参数可以留在服务目录，例如 `services/runtime/config/settings.toml`。

这样配置源保持中心化，同时两个服务可以独立部署、独立实现配置模型。

