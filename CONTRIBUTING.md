# 贡献指南

感谢你对 CaseWeave 的兴趣！本文说明如何在本地跑起来、以及提交改动的约定。

## 本地开发

```bash
cp .env.example .env         # 至少填 LLM_API_KEY / LLM_MODEL / LLM_PROVIDER
make install                 # 安装前后端依赖
make db-up                   # 只起 PostgreSQL(pgvector)
make dev-backend             # FastAPI on :8001
make dev-frontend            # Vite on :5173
```

或者 `make up` 用 Docker 一把起全套（含本地 embedding 服务）。

架构总览与各模块职责见 [CLAUDE.md](CLAUDE.md)，逐次迭代的详细过程记录在 `record.txt`。

## 提交前自检

CI 会跑这些，本地先过一遍能省一轮往返：

```bash
make lint                                   # ruff + eslint
cd backend && .venv/bin/python -c "import app.main"   # import 自检
cd frontend && npm run build                # 含 tsc 类型检查
```

## 约定

- **分支**：从 `main` 切出来，命名用 `feat/xxx`、`fix/xxx`。
- **提交信息**：`类型: 简述`，类型取 `feat` / `fix` / `docs` / `refactor` / `chore`。正文用中文没问题。
- **数据库变更**：改了 `backend/app/models/` 就要在 `backend/alembic/versions/` 里配套加迁移，文件名沿用 `000N_描述` 的序号格式。
- **Prompt 与 Skill**：都存在数据库里（不是文件），改默认 prompt 请动 `backend/app/prompts/` 中的常量。
- **不要提交**：`.env`、真实需求文档（`*.docx` / `*.pdf` 已在 `.gitignore` 里）、数据库导出、任何含内部业务信息的样例数据。

## 报告问题

提 Issue 时请附上复现步骤、后端日志片段（`backend/logs/app.log`），以及你用的 LLM provider / 模型。
涉及安全问题请勿公开提 Issue，见 [SECURITY.md](SECURITY.md)。
