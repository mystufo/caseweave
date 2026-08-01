# 安全策略

## 报告漏洞

发现安全问题请**不要**提公开 Issue。请通过 GitHub 的
[Private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
（仓库 Security 页签 → Report a vulnerability）提交，我们会尽快跟进。

## 自建部署须知

本项目默认面向内网/小团队部署，上线前请至少确认：

- `JWT_SECRET` 已设为随机值（`openssl rand -hex 32`）。留空或沿用占位值时后端会生成进程内
  随机密钥并打警告——重启即掉登录态，多 worker 下登录不可用。
- `DEBUG=false`（`.env.example` 里默认 `true`，会打开 SQL echo 与详细日志）。
- `CORS_ORIGINS` 只列实际的前端地址，不要用通配。
- `docker-compose.yml` 里 PostgreSQL 的默认账密是 `caseweave/caseweave`，且默认把 5433 端口
  暴露到宿主机——公网机器请改密码并去掉端口映射。
- `.env` 里的 `LLM_API_KEY`、飞书 `LARK_APP_SECRET` 等凭证不要提交进仓库。
