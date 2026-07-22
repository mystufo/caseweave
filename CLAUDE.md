# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**CaseWeave（纬策）** — 智能测试用例生成系统。用户上传产品需求文档（Word/PDF），系统先澄清歧义、再生成可执行测试用例（Excel）。具备持续自我进化能力（用户反馈、Bug数据、Browser Agent探索）。

## Commands

```bash
# Start all services (Docker)
make up

# Local development (requires .env and running PostgreSQL)
make dev-backend    # FastAPI on :8001
make dev-frontend   # Vite on :5173

# Only bring up PostgreSQL
make db-up

# Install dependencies
make install

# Lint
make lint
```

Copy `.env.example` to `.env` and fill in `LLM_API_KEY` before running.

## Architecture

**Frontend** (`frontend/`) — React + TypeScript + Vite + Tailwind CSS v3
- `src/pages/ChatPage.tsx` — main page (session list + chat + upload + test case table)
- `src/components/` — ChatMessage, MessageInput, SessionList, ClarificationPanel, TestCaseTable
- `src/api/client.ts` — all API calls + `streamChat()` SSE helper

**Backend** (`backend/app/`) — FastAPI + SQLAlchemy (async) + asyncpg
- `main.py` — app entry, CORS, router registration
- `config.py` — pydantic-settings, reads `.env`
- `database.py` — async engine, `get_db()` dependency, `init_db()` creates tables on startup
- `api/` — route modules: `routes_chat`, `routes_upload`, `routes_generate`, `routes_knowledge`, `routes_feedback`
- `agents/clarifier.py` — LLM call to identify ambiguities in a document, returns JSON question list
- `agents/generator.py` — LLM call to produce structured test case JSON array
- `tools/doc_parser.py` — `.docx` via python-docx, `.pdf` via pdfplumber
- `tools/excel_export.py` — openpyxl export, per-module sheets, frozen header

**Database** — PostgreSQL + pgvector; tables auto-created via `init_db()`, schema changes via Alembic migrations (`backend/alembic/versions/`)
- Key models: `Session`, `Message` (chat history), `TestCase`, `Feedback` (+ `reason`/`triage`/`triage_targets` for evolution triage), `FeedbackConsumption` (consumption ledger), `Module`, `KnowledgeEntry`, `Skill`, `PromptVersion`, `PromptSuggestion`, `Document`

**Data flow for core use case:**
1. `POST /api/upload` → parse doc → run Clarifier Agent → return questions
2. Frontend shows `ClarificationPanel`; user answers all questions
3. `POST /api/generate` → run Generator Agent with doc + answers → persist `TestCase` rows → return list
4. `GET /api/export/{session_id}` → stream Excel download
5. `POST /api/feedback` — records like/dislike/edit; edit type also updates the `TestCase` row

**Chat streaming:** `/api/chat` returns `text/event-stream` (SSE). Events: `{type:"session"}`, `{type:"text"}`, `{type:"done"}`, `{type:"error"}`.

## Development Phases

| Phase | Status | Scope |
|-------|--------|-------|
| 1 — Scaffold | ✅ Done | Project skeleton, DB models, basic streaming chat |
| 2 — Core flow | ✅ Done | Doc upload, Clarifier Agent, Generator Agent, Excel export, Feedback |
| 3 — Knowledge system | ✅ Done | pgvector semantic search, background knowledge extraction, doc accumulation, knowledge injection into Clarifier/Generator |
| 4 — Feedback evolution | ✅ Done | 4.1: edit-diff analysis (`diff_analyzer`), rule distillation, Skill CRUD + per-module auto-generation, Skill injection into Generator. 4.2 stage 1: prompt versioning (PromptVersion table, per-project, manual edit/activate via UI). 4.2 stage 2: system-generated prompt-improvement suggestions for `generator` — `PromptSuggestion` draft table, `prompt_optimizer` agent (feedback-driven, contract-guarded), manual + periodic-background generation (drafts only, never auto-activate), human review/adopt via `PromptManagerDrawer`. Adopting a suggestion still goes through the existing versioning API |
| 5 — Browser Agent | Pending | Playwright-based product exploration |
| 6 — Zentao MCP + Dashboard | Pending | Bug analysis, regression case generation, stats dashboard |

See `record.txt` (repo root) for the authoritative, detailed progress log. Cross-cutting infra already shipped (not a numbered phase): JWT auth (`auth.py`/`routes_auth.py`), multi-project isolation (`Project` model/`routes_projects.py`), Lark doc import (`tools/lark_fetcher.py`), mindmap paste import.

## LLM Provider switching

Controlled by `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` in `.env`.  
Agents in `backend/app/agents/` construct the LLM client directly — to add GLM support, replace `ChatAnthropic` with `ChatZhipuAI` and gate on `settings.llm_provider`.

## Prompts & Skills

Both are now **DB-backed**, not file-based — the `prompts/` and `skills/` directories are empty.

- **Prompts** — 3 system prompts (`clarifier_initial`, `clarifier_followup`, `generator`) are versioned in the `PromptVersion` table, isolated per project. `backend/app/prompts/registry.py` registers the logical keys; the in-code constants serve as each key's default ("原始建议版本"). Runtime loads the project's active version, falling back to the default constant when none is set. Agent functions take a `system_prompt` parameter injected by the route layer.
- **Skills** — reusable test-design knowledge per module, stored in the `Skill` table (CRUD + LLM auto-generation via `agents/skill_generator.py`), injected into the Generator prompt.
