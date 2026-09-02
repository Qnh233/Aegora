# Strapi Content Types

这些集合由 Strapi/MySQL 作为运营主库：

- `faq`、`skill`：发布后单向同步到 PostgreSQL 检索副本，再生成向量。
- `chat-session`、`chat-message`、`message-feedback`：Agent 运行时直接通过 Strapi Content API 读写。

优先运行：

```bash
PYTHONPATH=src python scripts/strapi_schema.py check
PYTHONPATH=src python scripts/strapi_schema.py create
```

Strapi 的 Content-Type Builder API 在生产模式可能关闭，普通 Content API Token 也通常
没有建类型权限。自动创建失败时，把对应目录的 `schema.json` 放到 Strapi 项目：

```text
src/api/<singular-name>/content-types/<singular-name>/schema.json
```

然后重启 Strapi，并为 Agent API Token 开放这些集合的 `find`、`findOne`、`create`、
`update` 权限。FAQ 若公司现有字段或路由不同，应以现有集合为准，只需调整
`config/settings.toml` 中的 `strapi_endpoint` 和同步字段映射。
