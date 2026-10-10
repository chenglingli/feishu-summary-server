# 飞书个人会话总结助手 · 后端

Python 3.12 / FastAPI / PostgreSQL / SQLAlchemy / Alembic。前端位于相邻的 `feishu-summary-web` 仓库。

**当前交付是可运行的首个开发版本，已验证两名演示用户的网页—API—任务闭环。真实飞书、模型和生产部署尚未验收。** 默认关闭真实消息处理。详细差距见 [实现状态](docs/IMPLEMENTATION_STATUS.md)。不要把演示任务成功等同于真实消息采集或发送成功。

## 本地启动

安装 Python 3.12、uv、Docker（仅运行本地 PostgreSQL 时需要）。

```bash
cp .env.example .env
uv sync --locked
docker compose -f deploy/compose.local.yaml up -d
uv run alembic upgrade head
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

第二个终端在本仓库运行：

```bash
uv run python -m app.runner
```

第三个终端在前端仓库运行 `npm ci && npm run dev`。打开首页 <http://localhost:5173> 选择演示账号，或使用下列入口；测试隔离时使用不同浏览器 Profile/隐身窗口：

- 员工 A：<http://localhost:5173/auth/dev/login/demo-a>
- 员工 B：<http://localhost:5173/auth/dev/login/demo-b>
- 管理员：<http://localhost:5173/auth/dev/login/demo-admin>

演示登录仅在 `APP_ENV=development`、`MOCK_AUTH_ENABLED=true` 且 PUBLIC_BASE_URL 为本机时开启。开发服务器只监听本机。演示任务明确显示 `DEMO_NOT_SENT`，送达状态保持 `not_started`。暂停允许手动总结；停用删除业务凭证，退出网页登录不影响每日计划。

没有 Docker 时可使用已有 PostgreSQL，设置 `DATABASE_URL` 后执行迁移。不要将测试数据库与开发/真实业务数据库混用。

## 测试与契约

先创建名字为 `summary_test` 的专用 PostgreSQL 数据库，测试会清空其中的应用表：

```bash
export DATABASE_URL='postgresql+psycopg://summary:summary@127.0.0.1:5432/summary_test'
uv run alembic upgrade head
uv run alembic check
uv run ruff check app tests
uv run ruff format --check app tests
uv run mypy app
uv run pytest -q
uv run python -m scripts.export_contract
```

提交 `contracts/openapi.json`；前端更新契约后运行类型生成，不手工维护响应字段。浏览器联调测试在前端仓库，要求独立、干净的本地演示库及 API、runner、Vite 均已启动。

## 真实联调配置

按 [技术设计](docs/飞书个人会话总结助手-技术设计-v0.1.md) 配置企业自建应用和两个 HTTPS 回调。设置真实 `ALLOWED_TENANT_KEY`、`PILOT_OPEN_IDS`、`ADMIN_OPEN_IDS`、App ID 以及密钥文件路径，关闭 MOCK。首次登录不会授予消息访问，用户需点击消息授权。

AES-GCM 密钥是 32 字节随机数的 Base64 编码，放入 `TOKEN_ENCRYPTION_KEY_FILE` 指向的文件；App Secret 和模型密钥也通过文件读取。不要将真实凭证放入文档、Git 或前端变量。模型接口目前适配 HTTPS OpenAI-compatible `/chat/completions`，`MODEL_BASE_URL` 包含供应商要求的版本前缀（如 `/v1`）。

`LIVE_PIPELINE_ENABLED=true` 仅供经过权限核验的受控联调，必须同时配置模型接口。当前采集对上下文/索引完整性保守标记 partial，不能作为“覆盖全部消息”验收结果。尚未读取本机现有飞书账号或替用户发送任何消息。

## 部署

`Dockerfile` 生成同一个后端镜像：API、runner 和一次性 migrate 共用。`deploy/compose.yaml` 是部署模板，不代表已经部署。配套 Nginx 配置由后端仓库统一维护，生产只有 443 暴露。

必须设置 FRONTEND_IMAGE、BACKEND_IMAGE、POSTGRES_IMAGE（均为明确镜像 digest）、APP_ENV_FILE、SECRETS_DIR、TLS_DIR。生产运行环境文件中的数据库连接使用 `postgres:5432`，Secret 文件路径使用 `/run/secrets/...`。目录挂载中的文件必须允许 UID 10001 读取。先启动 postgres，再执行 `docker compose -f deploy/compose.yaml --profile migration run --rm migrate`，成功后启动 api、runner、frontend。

runner 仅允许一个进程，内部最多两用户并发；不要扩副本。PostgreSQL advisory lock 用于单实例选主，任务另有租约、心跳与 fencing。升级要先进入维护、排空 runner，再迁移。主机应禁用 swap/core dump；正文与模型输出在进程内存中，固定发送载荷存于 runner 的 tmpfs，数据库仅保存元数据和加密凭证。容器重建会丢失 tmpfs；未知发送结果保持不确定。

前后端镜像已在本机构建通过。生产发布与恢复目标尚未验收，详见实现状态及 [运维文档](docs/OPERATIONS.md)。

## Git 与代码约定

两个仓库独立 Git 管理。每次提交使用主题和正文，正文说明改动原因、验证结果、未完成事项。提交前运行相应检查；锁文件、迁移、接口契约随代码提交。注释说明权限边界、幂等、凭证轮换及为何这样处理，不重复每行代码。当前文档以本仓库 `docs/` 为实现阶段入口。
