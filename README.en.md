# CaseWeave

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[简体中文](README.md) | **English**

> Weave requirements into test cases — an intelligent test-case generation system

**CaseWeave** is an AI-powered test-case generation system. Upload a product requirements document (Word / PDF / Feishu doc), and the system first **clarifies ambiguities** in the requirements, then generates structured, executable **test cases** and exports them to Excel. It also **continuously self-evolves**: distilling test-design experience from user feedback, edits, and bug data to improve later generations.

## Features

- 📄 **Document parsing** — Word / PDF upload, plus Feishu docs and mind-map paste import
- ❓ **Requirement clarification** — before generating, a Clarifier Agent surfaces ambiguities in the document and asks the user
- 🧪 **Case generation** — a Generator Agent produces structured test cases, exported to Excel with one sheet per module
- 🧠 **Knowledge base** — pgvector-based semantic search that accumulates product knowledge across documents and injects it into generation
- 🔁 **Feedback-driven evolution** — analyzes likes/dislikes/edit diffs to distill test-design rules (Skills) and propose prompt improvements
- 👥 **Multi-project isolation + JWT auth** — supports independent use across teams and projects

## Tech stack

- **Frontend**: React + TypeScript + Vite + Tailwind CSS
- **Backend**: FastAPI + SQLAlchemy (async) + asyncpg
- **Database**: PostgreSQL 16 + pgvector
- **LLM**: pluggable provider (official Anthropic API, or any OpenAI-compatible gateway such as Volcengine Ark / DeepSeek)

## Quick start

```bash
# 1. Configure environment variables
cp .env.example .env
# Edit .env; at minimum set LLM_API_KEY / LLM_MODEL / LLM_PROVIDER

# 2. One-command start with Docker (PostgreSQL + backend + frontend)
make up

# Open http://localhost:3001 and register an admin account
# (the email must be listed in ADMIN_EMAILS in .env)
```

Local development (requires Python 3.11, Node 20, and a running PostgreSQL):

```bash
make install       # install frontend & backend dependencies
make db-up         # start PostgreSQL only
make dev-backend   # FastAPI on :8001
make dev-frontend  # Vite on :5173
```

## Deployment

For production deployment (Docker Compose, or bare metal + systemd/Nginx), see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** (in Chinese).

## Documentation

- [Deployment guide](docs/DEPLOYMENT.md)
- [Feedback evolution design](docs/feedback-evolution-design.md)
- Development guide & architecture: [CLAUDE.md](CLAUDE.md)

## Contributing

Issues for bug reports and feature requests are welcome, as are pull requests.

## License

Released under the [MIT License](LICENSE).
