# CaseWeave 纬策 —— Ubuntu 部署文档

本文档说明如何在一台 Ubuntu 服务器上部署 CaseWeave 纬策（智能测试用例生成系统）。

系统由三个服务组成：

| 服务 | 说明 | 端口 |
|------|------|------|
| `db` | PostgreSQL 16 + pgvector（语义检索） | 5432 |
| `backend` | FastAPI + SQLAlchemy(async)，提供全部 API | 8001 |
| `frontend` | React 构建产物，由 Nginx 托管，并反向代理 `/api/` 到 backend | 3001（映射容器 80） |

推荐用 **Docker Compose 一键部署**（方式 A）。若服务器不便安装 Docker，可用**裸机部署**（方式 B）。

---

## 0. 前置准备（两种方式通用）

### 0.1 服务器要求

- Ubuntu 20.04 / 22.04 / 24.04（x86_64 或 arm64）
- 至少 2 vCPU / 4 GB 内存 / 20 GB 磁盘（LLM 请求走外部 API，本机不跑模型，压力主要在 Postgres 与并发请求）
- 可访问外网（需调用 LLM Provider 的 API；如用私有网关请确保网络可达）

### 0.2 需要提前准备的信息

- **LLM 凭证**：`LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY`（以及自建网关时的 `LLM_BASE_URL`）
- **Embedding 凭证**（可选，Phase 3 知识库语义检索用）：`EMBEDDING_*`。留空则知识检索静默降级为按时间倒序，不影响主流程
- **服务器对外地址**：下文用 `SERVER_IP` 代指服务器 IP 或域名（例如 `192.168.1.50` 或 `testcraft.example.com`）

### 0.3 关于数据库初始化（无需手动建表）

后端启动时会自动：
1. `CREATE EXTENSION IF NOT EXISTS vector`（启用 pgvector）
2. 建表（`init_db()`）+ 跑 Alembic 迁移到最新版本

所以**不需要手动执行任何建表 SQL 或迁移命令**。只要数据库用的是 pgvector 镜像（方式 A 已内置）或已手动装好 pgvector 扩展（方式 B）即可。

---

## 方式 A：Docker Compose 部署（推荐）

### A.1 安装 Docker 与 Compose 插件

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# 添加 Docker 官方源
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 可选：把当前用户加入 docker 组，免 sudo（需重新登录生效）
sudo usermod -aG docker $USER

# 验证
docker --version && docker compose version
```

### A.2 拉取代码

```bash
cd /opt        # 或任意你习惯的目录
git clone <你的仓库地址> case_generate_claude
cd case_generate_claude
```

### A.3 配置后端环境变量 `.env`

```bash
cp .env.example .env
```

编辑 `.env`，**至少修改以下项**（其余保持默认即可）：

```ini
# ── LLM（必填）──────────────────────────────────────
LLM_PROVIDER=openai                 # anthropic | openai(兼容协议，如火山方舟/DeepSeek)
LLM_MODEL=your-model
LLM_API_KEY=你的key
LLM_BASE_URL=https://your-gateway/v1   # 用官方 Anthropic 可留空

# ── 数据库（Docker 下保持默认，指向 compose 里的 db 服务）──
DATABASE_URL=postgresql+asyncpg://testcraft:testcraft@db:5432/testcraft

# ── CORS：必须包含前端对外访问地址，否则浏览器会拦截请求 ──
CORS_ORIGINS=http://SERVER_IP:3001

# ── Auth（生产务必修改）─────────────────────────────
ADMIN_EMAILS=you@example.com        # 只有这些账号能创建/删除项目
JWT_SECRET=用一段足够长的随机字符串替换   # 例如 openssl rand -hex 32
JWT_EXPIRE_HOURS=168

DEBUG=false
```

> **重要**：把上面的 `SERVER_IP` 换成实际服务器 IP 或域名。如果通过域名+反向代理走 HTTPS，`CORS_ORIGINS` 要填 `https://你的域名`。

Embedding（可选，启用知识库语义检索）：

```ini
EMBEDDING_API_KEY=你的embedding-key
EMBEDDING_BASE_URL=https://your-gateway/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIM=1536                  # 必须与模型输出维度一致
```

### A.4 配置前端后端地址（关键，别漏）

前端是**构建期**把后端地址烧进静态文件的。默认值是 `http://localhost:8001`，如果不改，用户浏览器会去访问自己电脑的 8001 端口 → 全部请求失败。

在 `frontend/` 目录下创建 `.env` 文件：

```bash
echo "VITE_API_URL=http://SERVER_IP:8001" > frontend/.env
```

（同样把 `SERVER_IP` 换成真实地址。Vite 构建时会自动读取该文件。）

> 若你后面按 A.7 配置了统一的 Nginx 反向代理让前后端同源，则应把它设为对外根地址（例如 `https://testcraft.example.com`），并由外层 Nginx 把 `/api/` 转发到 8001。

### A.5 构建并启动

```bash
docker compose up --build -d
# 或（项目提供了 Makefile 快捷方式）
make up
```

查看状态与日志：

```bash
docker compose ps
docker compose logs -f            # 或 make logs
docker compose logs -f backend    # 只看后端
```

首次启动 backend 会自动建库、跑迁移，日志里出现 `alembic upgrade head: ok` 即成功。

### A.6 验证

```bash
# 后端健康（应返回 JSON）
curl http://SERVER_IP:8001/docs -I    # FastAPI 文档页

# 前端
curl http://SERVER_IP:3001 -I         # 应 200
```

浏览器打开 `http://SERVER_IP:3001`，注册管理员账号（邮箱须在 `ADMIN_EMAILS` 中）即可创建项目、上传文档、生成用例。

### A.7 （可选）加一层 Nginx 做同源 + HTTPS

上面的做法前端(3001) 直接调后端(8001)，需要两个端口都对外开放且配好 CORS。生产更推荐用一个对外 Nginx 统一入口、同源访问、上 HTTPS：

```nginx
# /etc/nginx/sites-available/testcraft
server {
    listen 80;
    server_name testcraft.example.com;

    location / {
        proxy_pass http://127.0.0.1:3001;   # 前端容器
        proxy_set_header Host $host;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8001/api/;   # 后端容器
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;      # SSE 流式必须
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

此时：
- `frontend/.env` 设为 `VITE_API_URL=http://testcraft.example.com`（或 https 域名）
- `.env` 里 `CORS_ORIGINS=http://testcraft.example.com`
- 用 `certbot --nginx` 申请证书上 HTTPS
- docker-compose 里可把 3001、8001 只绑到 `127.0.0.1`，不直接对公网暴露

---

## 方式 B：裸机部署（不使用 Docker）

适用于服务器已有 Python/Postgres 环境、或不能用 Docker 的情况。仓库已提供 `deploy.sh`。

### B.1 安装系统依赖

```bash
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3.11-dev \
    build-essential libpq-dev git curl

# Node.js 20（前端构建用）
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# PostgreSQL 16 + pgvector
sudo apt-get install -y postgresql-16 postgresql-16-pgvector
# 若源里没有 pgvector 包，可参考 https://github.com/pgvector/pgvector 从源码编译
```

### B.2 初始化数据库

```bash
sudo -u postgres psql <<'SQL'
CREATE USER testcraft WITH PASSWORD 'testcraft';
CREATE DATABASE testcraft OWNER testcraft;
\c testcraft
CREATE EXTENSION IF NOT EXISTS vector;
SQL
```

（表结构后端启动时会自动创建，无需手动建表。）

### B.3 拉代码 + 配置 `.env`

```bash
cd /opt && git clone <你的仓库地址> case_generate_claude && cd case_generate_claude
cp .env.example .env
```

编辑 `.env`，与方式 A 相同，但 `DATABASE_URL` 改为本机：

```ini
DATABASE_URL=postgresql+asyncpg://testcraft:testcraft@localhost:5432/testcraft
CORS_ORIGINS=http://SERVER_IP        # 前端由 Nginx 托管在 80，则填 http://SERVER_IP
```

前端地址：

```bash
echo "VITE_API_URL=http://SERVER_IP:8001" > frontend/.env
```

### B.4 安装依赖 + 构建前端

```bash
# 后端虚拟环境 + 依赖（用锁定版本，保证与验证过的环境一致）
make install-prod

# 前端构建
cd frontend && npm ci && npm run build && cd ..
# 产物在 frontend/dist/
```

### B.5 用 systemd 托管后端

创建 `/etc/systemd/system/testcraft-backend.service`：

```ini
[Unit]
Description=CaseWeave backend
After=network.target postgresql.service

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/case_generate_claude/backend
EnvironmentFile=/opt/case_generate_claude/.env
ExecStart=/opt/case_generate_claude/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> 注意：`.env` 位于仓库根目录，而后端 `WorkingDirectory` 在 `backend/`。这里用 `EnvironmentFile` 显式加载根目录的 `.env`，确保变量被读到。

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now testcraft-backend
sudo systemctl status testcraft-backend
journalctl -u testcraft-backend -f     # 看日志
```

### B.6 用 Nginx 托管前端 + 反代 API

创建 `/etc/nginx/sites-available/testcraft`：

```nginx
server {
    listen 80;
    server_name SERVER_IP;

    root /opt/case_generate_claude/frontend/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8001/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffering off;      # SSE 流式响应必须关缓冲
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/testcraft /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

此时前端在 80 端口，若走同源（`/api/` 由 Nginx 反代），可把 `frontend/.env` 设为 `VITE_API_URL=http://SERVER_IP` 并重新 `npm run build`，`CORS_ORIGINS` 相应改为 `http://SERVER_IP`。

### B.7 后续更新

```bash
cd /opt/case_generate_claude
./deploy.sh                              # git pull + 装依赖 + 构建前端
sudo systemctl restart testcraft-backend # 按 deploy.sh 末尾提示重启
```

---

## 1. 升级与维护（Docker 方式）

```bash
cd /opt/case_generate_claude
git pull --ff-only
docker compose up --build -d      # 重新构建变更的镜像并滚动重启
docker compose logs -f backend
```

数据库迁移随后端启动自动执行（Alembic upgrade head）。

## 2. 数据备份

数据全部在 Postgres（用例、反馈、知识库、Prompt 版本等）。

```bash
# Docker 方式
docker compose exec db pg_dump -U testcraft testcraft > backup_$(date +%F).sql

# 裸机方式
pg_dump -U testcraft -h localhost testcraft > backup_$(date +%F).sql

# 恢复
cat backup_xxxx.sql | docker compose exec -T db psql -U testcraft testcraft
```

Docker 数据卷为 `pg_data`（`docker volume ls` 可见），删除 compose 时用 `docker compose down`（不加 `-v`）不会删数据；`docker compose down -v` 会**删除数据库卷**，谨慎使用。

## 3. 生产环境注意事项

- **务必修改 `JWT_SECRET`**（`openssl rand -hex 32` 生成），并设置正确的 `ADMIN_EMAILS`。
- **数据库密码**：默认 `testcraft/testcraft` 仅供开发，生产请改强密码，并同步更新 `docker-compose.yml` 的 `POSTGRES_PASSWORD` 与 `.env` 的 `DATABASE_URL`。
- **后端热重载**：当前 `backend/Dockerfile` 的启动命令带 `--reload` 且 compose 挂载了源码目录（便于开发）。生产环境建议：
  - 去掉 `docker-compose.yml` 中 backend 的 `volumes` 挂载（`./backend:/app` 等）；
  - 把 Dockerfile 的 `CMD` 改为不带 `--reload`，并可加 `--workers 2`（如 `uvicorn app.main:app --host 0.0.0.0 --port 8001 --workers 2`）。
- **防火墙**：仅对外开放需要的端口。走 A.7/B.6 的统一 Nginx 时，只需放行 80/443，把 8001、5432 限制在本机。
- **LLM 超时**：文档较长、用思考型模型时，若出现「正在抽取产品知识…」一直转圈，调大 `.env` 的 `LLM_TIMEOUT_SECONDS`。
- **飞书文档导入**（可选功能）：依赖 `lark-cli`，需在后端环境中另行安装并配置 `LARK_CLI_PATH`；不使用该功能可忽略。

## 4. 常见问题

| 现象 | 排查 |
|------|------|
| 前端页面能打开但所有请求失败/跨域报错 | `frontend/.env` 的 `VITE_API_URL` 是否为真实服务器地址；`.env` 的 `CORS_ORIGINS` 是否包含前端访问地址；改完前端需**重新构建** |
| 后端启动报 pgvector 相关错误 | 数据库是否为 pgvector 镜像 / 是否执行过 `CREATE EXTENSION vector` |
| 后端日志 `alembic upgrade failed` | 非致命（服务仍会起），但需关注；通常是数据库连接或历史 schema 问题，查 `journalctl` / `docker compose logs backend` |
| 上传大文档超时 | 调大 `LLM_TIMEOUT_SECONDS`；确认 Nginx `proxy_read_timeout` 足够（已给 300s） |
| SSE 流式（聊天/生成进度）不流式、一次性返回 | 反代必须 `proxy_buffering off`（本文的 Nginx 配置已包含） |
