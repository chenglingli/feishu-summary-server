# 本机真实接入配置

后端读取仓库根目录 `.env`；该文件被 Git 忽略。修改后需重启 API 和 runner。启动终端中已有的同名环境变量会优先于 `.env`，切换时应清除旧 Demo 变量。

## 密钥文件

`.env` 中的三个 `*_FILE` 项必须为绝对路径：

- `FEISHU_APP_SECRET_FILE`：文件内容只填应用 App Secret，不加变量名或引号。
- `MODEL_API_KEY_FILE`：文件内容只填模型 API Key，不加 `Bearer` 前缀。
- `TOKEN_ENCRYPTION_KEY_FILE`：32字节随机数的 Base64 编码；初始化生成后保持不变，不能随意替换，否则已保存的授权凭证无法解密。

文件权限使用0600，目录0700。密钥不提交 Git。尚未填写的外部密钥文件可为空，但此时不能开始真实登录/模型联调。

## 本机运行配置

- `APP_ENV=development`、`MOCK_AUTH_ENABLED=false`：本机真实身份联调，不使用演示身份。
- `LIVE_PIPELINE_ENABLED=false`：先验证登录和授权，确认权限及模型调用后再开启任务。
- `PUBLIC_BASE_URL`：当前 ngrok HTTPS origin，与飞书的两个回调地址及 Vite 域名允许列表一致。
- `DATABASE_URL`：使用独立的 `summary_live` 数据库；不要清空或复用 `summary_demo`，更不能用真实库运行 pytest。
- `PAYLOAD_DIR`：本机可写的0700临时目录。macOS `/private/tmp` 是磁盘临时目录，不是生产 tmpfs；真实发送前应明确本机临时载荷策略。生产部署使用已配置的容器 tmpfs。

数据库准备完成后，在仓库内运行 `.venv/bin/alembic upgrade head`、`.venv/bin/alembic check`。本机 API 启动命令为 `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log`，另一个终端运行 `.venv/bin/python -m app.runner`。仅保留一个 runner。

飞书后台还需自行完成应用权限审批、发布、可用范围和 OAuth 回调配置。填写本机文件不会自动修改飞书后台配置。

## Kimi K3 接入

`MODEL_ID=kimi-k3` 时，使用模型支持的 `reasoning_effort=low`，省略不允许自定义的 temperature 参数。参数依据：https://platform.kimi.com/docs/api/models-overview 。2026-10-09 已用虚构消息完成一次真实结构化总结与来源验证；这不等同于真实聊天质量或完整采集验收。

本机真实配置已启用飞书登录，应用凭证和模型列表检查通过；Demo 入口返回404。真实任务开关仍关闭，需用户亲自完成网页登录及独立的消息授权后再继续联调。尚未读取真实聊天或向用户发送总结。

### 首次真实手动任务准备

2026-10-09：首位试点用户已完成真实登录及消息授权，白名单和公司校验通过。因页面手动执行请求被关闭的 `LIVE_PIPELINE_ENABLED` 拦截，已将本机配置切换为 true，并重启 API/runner。用户每日计划仍未启用；首次真实采集、模型总结和发送结果尚待手动任务验收。本次只修改被忽略的本机配置，未将其提交 Git。
