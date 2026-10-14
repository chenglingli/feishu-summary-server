# 飞书个人会话总结助手 · 后端

> `docs/` 为本机维护的文档目录，不随 Git 分发。下文指向该目录的链接仅在本机已有文档时可用；新克隆需另行复制文档。

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

## Mac mini 一条命令管理后台服务

完成 `uv sync --locked`、前端 `npm ci`、后端 `.env`/密钥配置、数据库迁移和 ngrok 认证后，在后端目录执行：

```bash
bash scripts/services.sh start
bash scripts/services.sh status
bash scripts/services.sh logs runner
bash scripts/services.sh stop
bash scripts/services.sh restart
```

`start` 后台运行 API、Vite 前端、ngrok 和 runner，退出 SSH 不会主动终止这些进程。默认前端在后端的同级 `feishu-summary-web` 目录；非同级时用 `SUMMARY_WEB_DIR=/绝对路径/feishu-summary-web bash scripts/services.sh start`。优先使用 Homebrew Node 22。脚本不安装依赖、不迁移数据库、不启动或停止 PostgreSQL，数据库需事先通过 Homebrew 启动。

首次使用前，请停止之前手动/nohup 启动的四个服务。脚本检测端口及常见外部 runner/ngrok 进程，但不会接管或停止它们。只停止它自己记录且启动时间/命令仍匹配的进程；runner 正在退出时最多等30秒，不强制杀死，不继续重启，稍后检查 status。重复 start 会跳过仍在运行的托管进程。

PID 文件及日志位于后端 `.local-services/`（已忽略，不提交），新建文件仅当前用户可读写。`logs` 显示全部服务最近40行，`logs api/web/runner/ngrok` 显示单个服务日志。日志追加保存，需要定期清理；分享日志前检查是否含认证或个人信息。进程运行并不保证 ngrok 已上线，应核对日志、API健康检查和公网首页。

这是当前 Vite 本地试点的后台启动方式，不是开机自启或进程崩溃自动恢复方案；Mac mini 仍需防休眠。需要这些能力时再配置 launchd。保留 `.local-services/processes.json`，否则脚本无法识别原托管进程。

### runner 运行日志

通过服务脚本启动后，用 `bash scripts/services.sh logs runner` 查看最近日志，或用 `tail -f .local-services/runner.log` 持续查看。日志为逐行 JSON，北京时间（Asia/Shanghai，`+08:00`）含时区；每条立即刷新，不需要 Python `-u`。

包括 `runner_starting/runner_ready`（启动）、`runner_status`（每5分钟运行状态）、`task_claimed/task_stage/task_finished`（任务及阶段）、`collection_progress`（采集数量，通常最多每5秒一条，阶段结束强制更新）、`model_call_started/model_response/model_call_succeeded/model_call_failed`（每次模型调用）、`task_retry`（重试）、`delivery_started/delivery_succeeded/delivery_failed`（发送分段）及正常退出记录。`run_id` 关联任务，`call_id` 区分模型调用，成功HTTP响应不等于输出校验通过。可按任务编号筛选，例如 `rg '任务UUID' .local-services/runner.log`。

日志只含允许的运行元数据，不记录聊天、总结、模型请求/响应正文、凭证、收件人或任意异常文本。不可从日志恢复历史正文；这次改动不补写过去任务。日志仍是追加文件，尚未配置自动轮转，长期运行需要定期管理大小。更新代码后需要重启 runner 才生效。

### 模型预算与截断诊断

模型请求先按输入 UTF-8 字节、输出上限和固定开销保守预留预算。响应中 `usage` 的 prompt/completion/total 为非负整数、总量大于零且相加一致时，用实际 token 总量替换该次预留；截断响应同样结算。缺失或异常 usage、网络失败保留原估算。推理 token 是 completion 的子集，不重复累计。实际用量超限时停止后续处理，不自动提高 `MODEL_TOKEN_BUDGET`。

日志新增 `model_usage`（actual/estimated、输入/输出/总 token、可用的推理 token、累计预算）及 `model_output`（结束原因和输出字符数）；请求开始记录输入字符数、输出上限和预留额度。不记录模型或推理正文。预算仍会在发送前保守检查，不能保证等同供应商账单。重启 runner 后生效。

### 网页长期登录

所有用户网页登录默认保持 90 天，取消闲置 30 分钟失效。`SESSION_LIFETIME_DAYS` 可配置为 1～365 天；数据库会话与浏览器 Cookie 使用相同有效期，从登录时计算，不因轮询无限延长。退出登录立即删除服务端会话，访问名单和账号状态仍逐次校验。此配置不改变飞书消息授权凭证的刷新与失效规则。

重启 API 后新登录生效；已有会话不追溯延长，请重新登录一次。清除 Cookie、切换域名或浏览器仍需要登录。

### Qwen 总结模型

当前网关可使用 `MODEL_ID=openai/qwen3.8-flash`，`MODEL_BASE_URL` 保持网关的 `/v1` 基础地址。代码对 `openai/qwen3.8-flash` 与 `qwen3.8-flash` 的提取、合并和重试请求显式发送 `enable_thinking: false`；其他模型不附带这个参数。更换配置后需重启 runner。现有分批、输出校验和任务预算继续生效。

### 合并失败时的报告恢复

分批提取必须通过结构与来源校验。之后的模型合并仅用于整理展示：若合并重试仍出现完整性、来源、格式、截断错误，或遇到临时服务/网络故障及预算不足，则舍弃该合并输出，保留对应批次已经验证的全部提取结果，再由本地代码分类去重。报告可能更长，语义相似内容可能重复，但不以未经校验的合并结果覆盖原始提取。日志记录 `model_merge_fallback` 及原因。鉴权、请求参数、内容策略与取消等错误仍向上传播；提取阶段预算不足不会被包装成完整成功。

总结采用尽力交付：提取批次发生可恢复模型错误时保留已验证结果并继续后续批次；预算不足则停止模型调用并发送已有结果。报告显式标记“部分总结”，任务覆盖说明添加 `SUMMARY_PARTIAL`。没有任何有效结果时发送未完成说明，不声称没有事项。撤销、鉴权和策略错误以及任务总时限仍按原规则处理。
