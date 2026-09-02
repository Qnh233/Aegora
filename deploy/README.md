# Deployment & CI/CD

Aegora 从此目录维护部署入口，避免 Control Plane 与 Runtime 各自复制一套基础设施配置。

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

## 当前最小 CI/CD 链路

当前阶段刻意保持简单，不引入 Kubernetes / ArgoCD：

```text
feature branch / PR
        |
        v
GitHub Actions: CI
  - pytest
  - frontend npm build
        |
        v
manual merge -> main
        |
        v
GitHub Actions: Build Images
        |
        v
GHCR (Git SHA + main tags)
        |
        v
Deploy Staging (manual workflow)
        |
        v
SSH -> docker compose pull/up -> health checks
```

核心原则：

- PR 测试通过 **不会自动 merge**；当前仍由开发者确认后合并。
- `main` 每次更新自动构建三个镜像并推送到 GHCR。
- 镜像同时保留完整 Git SHA 与 `main` 标签；部署和回滚优先使用不可变 SHA。
- Staging 部署是手动触发，避免每次 push 都重启云端环境。
- 云服务器不现场构建源码，只从 GHCR 拉取已验证镜像。
- Production 自动部署暂不启用；等 staging 链路稳定后再增加审批 + Blue/Green。

## Workflows

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `.github/workflows/ci.yml` | PR -> `main` / manual | Python tests + frontend build |
| `.github/workflows/build-images.yml` | push `main` / manual | Build and push backend/frontend/runtime images to GHCR |
| `.github/workflows/deploy-staging.yml` | manual | Deploy a selected image SHA to staging and run health checks |

## GHCR images

假设 GitHub owner 为 `Qnh233`，Actions 会规范化为小写并生成：

```text
ghcr.io/qnh233/aegora-control-plane-backend:<git-sha>
ghcr.io/qnh233/aegora-control-plane-frontend:<git-sha>
ghcr.io/qnh233/aegora-runtime:<git-sha>
```

同时更新便利标签 `:main`。部署和回滚不要依赖 `:main`，而应指定明确 SHA。

## GitHub repository settings

建议给 `main` 开启 Branch Protection：

1. Require a pull request before merging.
2. Require status checks to pass before merging.
3. 将 `Python tests` 与 `Frontend build` 设为 required checks。
4. 当前不启用自动 merge；以后需要时再使用 GitHub 原生 Auto-merge。

`Build Images` 使用仓库自带的 `GITHUB_TOKEN` 写入 GHCR，不需要云服务器 SSH Key。

## Staging server one-time setup

服务器只需要 Docker、Docker Compose plugin、`curl`，并创建部署目录：

```bash
sudo mkdir -p /opt/aegora
sudo chown <deploy-user>:<deploy-user> /opt/aegora
cd /opt/aegora
```

然后在服务器创建 `/opt/aegora/.env`。该文件是 **服务器私有配置**，不要提交到 Git。可以从仓库根目录 `.env.example` 整理，但数据库、LLM、Embedding 等地址必须改成 staging 实际可访问的地址。

容器间 Runtime 调用由 compose 覆盖为：

```text
RUNNER_GATEWAY_URL=http://runtime:5000
```

数据库等持久服务当前不由这份 compose 自动创建，避免应用 CI/CD 同时接管有状态基础设施。

## GitHub Environment: staging

在 GitHub 创建名为 `staging` 的 Environment，并添加：

```text
STAGING_HOST       云服务器 IP / hostname
STAGING_USER       专用部署用户，不建议使用 root
STAGING_SSH_KEY    部署用户私钥
```

Workflow 使用当前 Job 的短生命周期 `GITHUB_TOKEN` 登录 GHCR，并显式授予 `packages: read`；不需要额外保存长期 GHCR PAT。首版默认 SSH 端口为 `22`。部署用户需要能够：

- 写入 `/opt/aegora`；
- 执行 `docker` / `docker compose`；
- 访问 GHCR 与运行时所需的外部依赖。

## Deploy / rollback

正常发布：

1. PR CI 通过并手动 merge 到 `main`。
2. 等 `Build Images` 成功，记下该提交 SHA。
3. GitHub -> Actions -> `Deploy Staging` -> Run workflow。
4. `image_tag` 留空时使用当前选中 `main` 的 SHA；也可以显式输入目标 SHA。
5. Workflow 上传最新 compose，拉取三个同版本镜像，启动后检查 Runtime `/healthz`、Backend `/auth/config` 和 Web 首页。

回滚不重新构建：重新运行 `Deploy Staging`，把 `image_tag` 填成上一个稳定 Git SHA 即可。

## 下一阶段

等这条链路稳定后再增加：

1. Production GitHub Environment + required approval。
2. Blue/Green 两套应用实例与入口切换。
3. 自动记录 last-known-good SHA，并提供一键 rollback。
4. 根据 Monorepo 变更路径做增量 CI / 增量镜像构建。

