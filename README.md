# CaseWeave 纬策

[![Release](https://img.shields.io/github/v/release/mystufo/caseweave)](https://github.com/mystufo/caseweave/releases/latest)
[![CI](https://github.com/mystufo/caseweave/actions/workflows/ci.yml/badge.svg)](https://github.com/mystufo/caseweave/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**简体中文** | [English](README.en.md)

> 把需求织成用例 —— 智能测试用例生成系统

**CaseWeave（纬策）** 是一个 AI 驱动的测试用例生成系统。上传产品需求文档（Word / PDF / 飞书文档），系统先自动**澄清需求中的歧义**，再据此生成结构化、可执行的**测试用例**并导出 Excel。系统还具备**持续自我进化**能力：从用户反馈、编辑修改、Bug 数据中不断提炼测试设计经验，反哺后续生成。

## 核心特性

- 📄 **文档解析** — 支持 Word / PDF 上传，以及飞书文档、脑图粘贴导入
- ❓ **需求澄清** — 生成前先由 Clarifier Agent 识别文档中的歧义并向用户提问
- 🧪 **用例生成** — Generator Agent 产出结构化测试用例，按模块分 Sheet 导出 Excel
- 🧠 **知识库** — 基于 pgvector 的语义检索，跨文档积累产品知识并注入生成过程
- 🔁 **反馈进化** — 分析用户的点赞/点踩/编辑差异，蒸馏测试设计规则（Skill），并给出 Prompt 改进建议
- 👥 **多项目隔离 + JWT 鉴权** — 支持多团队/多项目独立使用

## 技术栈

- **前端**：React + TypeScript + Vite + Tailwind CSS
- **后端**：FastAPI + SQLAlchemy(async) + asyncpg
- **数据库**：PostgreSQL 16 + pgvector
- **LLM**：可切换 Provider（Anthropic 官方 API，或 OpenAI 兼容协议网关，如火山方舟 / DeepSeek 等）

## 快速开始

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填入 LLM_API_KEY / LLM_MODEL / LLM_PROVIDER

# 2. Docker 一键启动（PostgreSQL + 后端 + 前端）
make up

# 打开 http://localhost:3001，注册管理员账号（邮箱须在 .env 的 ADMIN_EMAILS 中）
```

本地开发（需已装 Python 3.11、Node 20，并启动 PostgreSQL）：

```bash
make install       # 安装前后端依赖
make db-up         # 仅启动 PostgreSQL
make dev-backend   # FastAPI on :8001
make dev-frontend  # Vite on :5173
```

## 部署

生产部署（Docker Compose 或裸机 + systemd/Nginx）详见 **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**。

## 文档

- [部署文档](docs/DEPLOYMENT.md)
- [反馈进化设计](docs/feedback-evolution-design.md)
- 开发指引与架构说明见 [CLAUDE.md](CLAUDE.md)

## 贡献

欢迎提 Issue 反馈问题或需求，也欢迎提交 Pull Request。开发环境搭建、提交约定见 **[CONTRIBUTING.md](CONTRIBUTING.md)**。

安全问题请不要走公开 Issue，见 **[SECURITY.md](SECURITY.md)**。自建部署上线前，务必按其中的清单设好 `JWT_SECRET`、关掉 `DEBUG`、改掉数据库默认账密。

## 交流

对项目感兴趣、想交流测试用例生成或 Agent 相关实践的，欢迎联系我：

- 微信：**mystufo**（加好友请注明「CaseWeave」）
- 邮箱：**mystufo@aliyun.com**

## License

本项目采用 [MIT License](LICENSE) 开源。
