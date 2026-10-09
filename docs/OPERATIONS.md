# 运维命令与验收边界

## 健康监控

管理员可访问 `GET /api/admin/health`（仍需网页登录会话）。本机探针：

```bash
uv run python -m app.health
```

返回元数据 JSON；退出码 0 为正常，1 为心跳超过90秒/存在超过10分钟任务/过去24小时存在送达不确定，2 为探针自身异常。维护状态单独返回。探针不发送通知，外部监控系统需配置执行频率和告警渠道。

## 配置备份

用独立的32字节随机备份密钥（Base64 文件），与凭证加密密钥分开保管。密钥文件不进 Git，备份文件写入权限0600。示例路径均为部署者自定：

```bash
uv run python -m app.backup backup --key-file /run/secrets/backup_key --path /var/backups/summary
```

采用 AES-GCM 加密和完整性校验，只保存身份标识及用户配置。不会保存业务 token、网页登录态、聊天内容、总结、任务历史。每次成功备份后清理所选目录内超过7天的 `config-*.enc` 普通文件；请使用专用目录。用系统定时服务每天执行，并监控退出码。不备份密钥本身，密钥丢失无法恢复。

## 恢复

先停止 API 和 runner；准备空数据库，升级到备份记录的同一 Alembic revision。数据库中有任何应用记录时拒绝恢复，不提供覆盖选项。设置维护模式后执行：

```bash
MAINTENANCE_MODE=true uv run python -m app.backup restore --key-file /run/secrets/backup_key --path /var/backups/summary/config-时间.enc
```

恢复在单事务内完成。所有用户需要重新消息授权，启用状态恢复为暂停（已停用者仍停用），不补跑停机历史。先核验公司与用户白名单，再启动服务、重新授权和恢复计划。实际生产恢复时间、异地备份、磁盘加密和告警路由需部署环境验收；本地测试仅证明配置往返及拒绝错误密钥/非空库/版本不符。
