.PHONY: up down logs dev-backend dev-frontend install install-backend install-frontend install-prod lock lint format venv

# ── venv ──────────────────────────────────────────────────────────────────────
# 与 backend/Dockerfile 的 python:3.11-slim 保持一致；
# 优先使用 python3.11，找不到则回退到 python3 并校验版本号。
PYTHON_BIN ?= $(shell command -v python3.11 2>/dev/null || command -v python3)
REQUIRED_PY := 3.11

VENV := backend/.venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
RUFF := $(VENV)/bin/ruff

$(VENV)/bin/activate:
	@if [ -z "$(PYTHON_BIN)" ]; then \
		echo "找不到 python3.11 或 python3，请先安装 Python $(REQUIRED_PY)（建议 pyenv install $(REQUIRED_PY) 或 brew install python@3.11）"; \
		exit 1; \
	fi
	@ver=$$($(PYTHON_BIN) -c 'import sys;print("%d.%d"%sys.version_info[:2])'); \
	if [ "$$ver" != "$(REQUIRED_PY)" ]; then \
		echo "✗ 检测到 Python $$ver，但项目要求 $(REQUIRED_PY)（与 Docker 镜像一致）"; \
		echo "  当前解释器：$(PYTHON_BIN)"; \
		echo "  解决方法：安装 python3.11 后重试，或显式指定 PYTHON_BIN=/path/to/python3.11"; \
		exit 1; \
	fi
	$(PYTHON_BIN) -m venv $(VENV)
	$(PIP) install --upgrade pip

venv: $(VENV)/bin/activate

# ── Docker ────────────────────────────────────────────────────────────────────
up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f

# ── Local development ─────────────────────────────────────────────────────────
dev-backend:
	cd backend && ../$(VENV)/bin/uvicorn app.main:app --reload \
		--reload-dir app --reload-include '*.py' \
		--host 0.0.0.0 --port 8001

dev-frontend:
	cd frontend && npm run dev

install: install-backend install-frontend

# 本地开发：装 requirements.txt（直接依赖），传递依赖随 pip 解析。
install-backend: $(VENV)/bin/activate
	$(PIP) install -r backend/requirements.txt

# 生产部署：装 requirements.lock.txt（全部依赖含传递依赖都锁版本），
# 保证服务器装出来的环境与本地完全一致。--require-hashes 暂未启用，
# 如需更严格的供应链校验可加 --hash 后开启。
install-prod: $(VENV)/bin/activate
	$(PIP) install --no-deps -r backend/requirements.lock.txt

# 重新生成 lock 文件（本地依赖变动后跑一次，提交到 git）
lock: $(VENV)/bin/activate
	$(PIP) install -r backend/requirements.txt
	$(PIP) freeze --exclude-editable > backend/requirements.lock.txt
	@echo "✓ backend/requirements.lock.txt 已更新，记得 git add + commit"

install-frontend:
	cd frontend && npm install

# ── Database ──────────────────────────────────────────────────────────────────
db-up:
	docker compose up db -d

# ── Lint / format ─────────────────────────────────────────────────────────────
lint:
	$(RUFF) check backend/app/
	cd frontend && npm run lint

format:
	$(RUFF) format backend/app/
