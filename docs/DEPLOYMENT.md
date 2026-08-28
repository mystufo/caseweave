# CaseWeave 纬策 —— Ubuntu 部署文档

本文档说明如何在一台 Ubuntu 服务器上部署 CaseWeave 纬策（智能测试用例生成系统）。

系统由三个服务组成：

| 服务 | 说明 | 端口 |
|------|------|------|
| `db` | PostgreSQL 16 + pgvector（语义检索） | 宿主机 5433 → 容器 5432（可配置，见下） |
| `backend` | FastAPI + SQLAlchemy(async)，提供全部 API | 8001 |
| `frontend` | React 构建产物，由 Nginx 托管，并反向代理 `/api/` 到 backend | 3001（映射容器 80） |

> **db 端口**：为避开宿主机上可能已存在的 PostgreSQL（5432），compose 默认把 db 容器映射到宿主机 **5433**（`"${DB_HOST_PORT:-5433}:5432"`）。可在 `.env` 用 `DB_HOST_PORT` 改端口。这只影响从宿主机/外部用工具连库；**容器间通信始终走内网 `db:5432`，与此无关**，所以 `DATABASE_URL` 里的 `@db:5432` 不用改。

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
- **服务器对外地址**：下文用 `SERVER_IP` 代指服务器 IP 或域名（例如 `192.168.1.50` 或 `caseweave.example.com`）

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

# 添加 Docker 官方源（这一步不能跳过：docker-ce 等包不在 Ubuntu 自带源里，
# 跳过后直接装会报 "Package docker-ce has no installation candidate"）
sudo install -m 0755 -d /etc/apt/keyrings

# 国内服务器把下面两处 https://download.docker.com/linux/ubuntu 换成
# https://mirrors.aliyun.com/docker-ce/linux/ubuntu（GPG 与 deb 行都要换，包内容一致）。
# 直连官方站常报 curl: (35) Recv failure: Connection reset by peer
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
CODENAME=$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
echo "codename = $CODENAME"     # 应为 focal / jammy / noble，为空或非这三者见下方说明框
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $CODENAME stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
apt-cache policy docker-ce      # 能看到候选版本号 = 源已生效，否则见下方说明框
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 启动 docker 守护进程并设为开机自启（装完不会自动起，漏了会在后面 up 时报
# "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"）
sudo systemctl enable --now docker
sudo systemctl status docker --no-pager

# 可选：把当前用户加入 docker 组，免 sudo（需重新登录生效）
sudo usermod -aG docker $USER

# 验证
docker --version && docker compose version
docker info | head -20        # 能输出 Server 段信息即守护进程正常
```

> **装不上 docker-ce（`no installation candidate` / `Unable to locate package containerd.io`）**：说明 Docker 源没生效，两个常见原因：
> - **发行代号不对**。上面用 `${UBUNTU_CODENAME:-$VERSION_CODENAME}` 而非直接用 `$VERSION_CODENAME`，是因为基于 Ubuntu 的衍生版（麒麟、Deepin、部分云厂商定制镜像）的 `VERSION_CODENAME` 是它自己的代号，Docker 源里没有对应目录，而 `UBUNTU_CODENAME` 才是 `jammy`/`noble`。纯 Ubuntu 上两者结果相同。若系统是 Debian，把两处 URL 的 `/ubuntu` 换成 `/debian`。
> - **连不上 `download.docker.com`**（国内服务器常见）。换阿里云镜像即可，包内容一致：把上面两处 `https://download.docker.com/linux/ubuntu` 换成 `https://mirrors.aliyun.com/docker-ce/linux/ubuntu`（GPG 与 deb 行都要换），重跑 `sudo apt-get update`。
>
> 实在都不通，可用发行版自带包兜底：`sudo apt-get install -y docker.io docker-compose-v2`（20.04/22.04 若无 `docker-compose-v2`，改用 `docker.io docker-buildx docker-compose-plugin`）。版本较旧但足够跑本项目；注意若只装到 v1 的 `docker-compose`，会踩 A.1.2 的 `ContainerConfig` 问题。

> **构建期依赖已内置国内源**：`backend/Dockerfile` 已把 apt 换阿里云 Debian 源、pip 换阿里云 PyPI 源；`frontend/Dockerfile` 已把 npm 换 npmmirror 源。无需额外配置。基础镜像的拉取加速见下一节。

### A.1.1 配置镜像加速器（国内服务器必做）

`docker compose up --build` 需要拉 `pgvector/pgvector:pg16`、`python:3.11-slim`、`node:20-alpine`、`nginx:alpine` 等基础镜像。国内直连 Docker Hub 基本拉不动，典型报错是域名被解析到无关 IP 后超时：

```
Error response from daemon: failed to resolve reference "docker.io/pgvector/pgvector:pg16":
failed to do request: Head "https://registry-1.docker.io/v2/...": dial tcp 31.13.76.65:443: i/o timeout
```

**装完 Docker 立刻配好加速器，别等 `up` 报错再回头配**：

```bash
# 注意：daemon.json 必须是合法 JSON，写坏会导致 dockerd 起不来（见下方说明框）
echo '{"registry-mirrors":["https://docker.m.daocloud.io","https://dockerproxy.net"]}' \
  | sudo tee /etc/docker/daemon.json

python3 -m json.tool /etc/docker/daemon.json    # 校验：能回显 JSON 即格式正确

sudo systemctl daemon-reload
sudo systemctl restart docker
docker info | grep -A3 "Registry Mirrors"       # 能列出镜像地址 = 已生效
```

> **阿里云 ECS 优先用专属加速地址**：控制台 → 容器镜像服务 → 镜像加速器，形如 `https://<你的ID>.mirror.aliyuncs.com`，走内网比公共站稳得多，把它放 `registry-mirrors` 数组第一位。公共加速站会挂、会限流。

> **加速器仍拉不到某个镜像**：带前缀显式拉取再打回原 tag，compose 就会直接用本地镜像：
> ```bash
> docker pull docker.m.daocloud.io/pgvector/pgvector:pg16
> docker tag  docker.m.daocloud.io/pgvector/pgvector:pg16 pgvector/pgvector:pg16
> ```
> 官方镜像要补 `library/`，如 `docker.m.daocloud.io/library/python:3.11-slim`。

> **改完 daemon.json 后 `systemctl restart docker` 失败**（`Job for docker.service failed` / `Start request repeated too quickly`）：几乎都是 daemon.json 不是合法 JSON——用 heredoc 写文件时若结束标记 `EOF` 前面带了空格，shell 会一直停在 `>` 续行提示符，容易把多余内容写进文件（所以上面用单行 `echo` 写法）。排查与修复：
> ```bash
> cat /etc/docker/daemon.json
> journalctl -u docker.service -n 50 --no-pager | grep -i "level=fatal\|unable to configure"
>
> # 重写为合法 JSON 后，必须先清失败计数再启动，
> # 否则 systemd 会因「重启太频繁」直接拒绝，即使配置已经改对
> sudo systemctl reset-failed docker
> sudo systemctl start docker
> ```
> 想先把 Docker 拉起来排除干扰：`sudo rm /etc/docker/daemon.json && sudo systemctl reset-failed docker && sudo systemctl start docker`。

> **embedding / rerank 镜像走的是 ghcr.io**（`ghcr.io/huggingface/text-embeddings-inference`，各约 1~2 GB），不受 Docker Hub 加速器影响。若 ghcr 也不通，`docker-compose.yml` 已把地址变量化，在 `.env` 里设 `EMBEDDING_IMAGE` / `RERANK_IMAGE` 指向可达的镜像源即可，不用改 compose 文件。

> **镜像拉下来了，还有第二道坎：模型权重**。TEI 容器首次启动会从 `huggingface.co` 下载 `BAAI/bge-m3` 与 `BAAI/bge-reranker-v2-m3`（各约 2 GB），国内直连基本不通，表现为容器起来了但一直没日志、后端语义检索始终降级。在 `.env` 里设 `HF_ENDPOINT=https://hf-mirror.com` 走国内镜像站，然后 `docker compose up -d embedding reranker` 重建容器生效。判断是否卡在这里：`docker compose logs -f embedding`，正常会看到下载进度，卡住则长时间无输出。

### A.1.2 关于 docker compose 版本

本文命令用 **v2 语法 `docker compose`（带空格）**。若服务器上是老的 **v1 `docker-compose`（带连字符，如 1.29.2）**，用它 `up`/`recreate` 时可能报 `KeyError: 'ContainerConfig'`——这是 v1 与新版 Docker 镜像格式不兼容的已知 bug，触发点是"就地重建旧容器"。绕过办法：**先 `docker-compose down` 删掉旧容器，再 `docker-compose up -d` 全新创建**（`down` 不删 `pg_data` 卷，数据安全）。长期建议装 v2 插件（`sudo apt-get install -y docker-compose-plugin`）改用 `docker compose`。

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
DATABASE_URL=postgresql+asyncpg://caseweave:caseweave@db:5432/caseweave

# ── CORS：必须包含前端对外访问地址，否则浏览器会拦截请求 ──
CORS_ORIGINS=http://SERVER_IP:3001

# ── Auth（生产务必修改）─────────────────────────────
ADMIN_EMAILS=you@example.com        # 只有这些账号能创建/删除项目
JWT_SECRET=用一段足够长的随机字符串替换   # 例如 openssl rand -hex 32
JWT_EXPIRE_HOURS=168

DEBUG=true
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

> 若你后面按 A.7 配置了统一的 Nginx 反向代理让前后端同源，则应把它设为对外根地址（例如 `https://caseweave.example.com`），并由外层 Nginx 把 `/api/` 转发到 8001。

### A.5 构建并启动

```bash
docker compose up --build -d
# 老版本用连字符：docker-compose up --build -d（若报 ContainerConfig 错，见 A.1.2）
# 等价快捷方式（Makefile 里就是上面这条命令，二选一即可，不必重复执行）：make up
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
# /etc/nginx/sites-available/caseweave
server {
    listen 80;
    server_name caseweave.example.com;

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
- `frontend/.env` 设为 `VITE_API_URL=http://caseweave.example.com`（或 https 域名）
- `.env` 里 `CORS_ORIGINS=http://caseweave.example.com`
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
CREATE USER caseweave WITH PASSWORD 'caseweave';
CREATE DATABASE caseweave OWNER caseweave;
\c caseweave
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
DATABASE_URL=postgresql+asyncpg://caseweave:caseweave@localhost:5432/caseweave
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

创建 `/etc/systemd/system/caseweave-backend.service`：

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
sudo systemctl enable --now caseweave-backend
sudo systemctl status caseweave-backend
journalctl -u caseweave-backend -f     # 看日志
```

### B.6 用 Nginx 托管前端 + 反代 API

创建 `/etc/nginx/sites-available/caseweave`：

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
sudo ln -s /etc/nginx/sites-available/caseweave /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

此时前端在 80 端口，若走同源（`/api/` 由 Nginx 反代），可把 `frontend/.env` 设为 `VITE_API_URL=http://SERVER_IP` 并重新 `npm run build`，`CORS_ORIGINS` 相应改为 `http://SERVER_IP`。

### B.7 后续更新

```bash
cd /opt/case_generate_claude
./deploy.sh                              # git pull + 装依赖 + 构建前端
sudo systemctl restart caseweave-backend # 按 deploy.sh 末尾提示重启
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

> **老部署升级注意（库名从 `testcraft` 改为 `caseweave`）**：本项目默认的数据库用户/库名已由 `testcraft` 统一为 `caseweave`。**你现有数据库里的数据不会被自动改动，也不会丢**——`docker-compose.yml` 的 `POSTGRES_*` 只在数据卷首次初始化时生效，卷已有数据时被忽略。但拉了新代码后，后端会用 `caseweave` 凭证去连仍叫 `testcraft` 的库而认证失败。二选一：
>
> **选项一：继续用 `testcraft`（最省事，不碰数据库）**
> - **Docker**：把 `docker-compose.yml` 里 backend 的 `environment.DATABASE_URL` 与 db 的 `POSTGRES_*` 改回 `testcraft`（compose 的 `environment` 优先级高于 `.env`，所以这里必须改）。
> - **裸机**：保持 `.env` 的 `DATABASE_URL=...testcraft...` 不变即可，无需任何操作。
>
> **选项二：跟随新命名并保住数据**（推荐先备份：`pg_dump -U testcraft testcraft > backup.sql`）
> 在库还叫 `testcraft` 时，把 **库名 + 角色名 + 密码** 三样都改成 `caseweave`，再上新代码：
>   ```bash
>   # Docker：先停 backend 释放连接，用旧凭证连 postgres 库执行 RENAME
>   docker compose stop backend
>   docker compose exec db psql -U testcraft -d postgres \
>     -c "ALTER DATABASE testcraft RENAME TO caseweave;" \
>     -c "ALTER ROLE testcraft RENAME TO caseweave;" \
>     -c "ALTER ROLE caseweave WITH PASSWORD 'caseweave';"
>   git pull --ff-only && docker compose up -d
>   ```
>   ⚠️ 第三条 `ALTER ROLE ... PASSWORD` 不能省：pg16 用 scram-sha-256，改名后密码仍是旧值，新连接串（`caseweave:caseweave`）会认证失败。
>
>   裸机方式同理：`sudo systemctl stop <你的服务>` 后，用 `sudo -u postgres psql -d postgres` 跑上面三条 `ALTER`，再把 `.env` 的 `DATABASE_URL` 改成 `caseweave`，并把 systemd 单元名从 `testcraft-backend` 改为 `caseweave-backend`（重新 `daemon-reload`）。
>
> `ALTER DATABASE RENAME` 是瞬时的元数据操作，表/数据/pgvector 向量/扩展全部原样保留。

## 2. 数据备份

数据全部在 Postgres（用例、反馈、知识库、Prompt 版本等）。

```bash
# Docker 方式
docker compose exec db pg_dump -U caseweave caseweave > backup_$(date +%F).sql

# 裸机方式
pg_dump -U caseweave -h localhost caseweave > backup_$(date +%F).sql

# 恢复
cat backup_xxxx.sql | docker compose exec -T db psql -U caseweave caseweave
```

Docker 数据卷为 `pg_data`（`docker volume ls` 可见），删除 compose 时用 `docker compose down`（不加 `-v`）不会删数据；`docker compose down -v` 会**删除数据库卷**，谨慎使用。

## 3. 生产环境注意事项

- **务必修改 `JWT_SECRET`**（`openssl rand -hex 32` 生成），并设置正确的 `ADMIN_EMAILS`。
- **数据库密码**：默认 `caseweave/caseweave` 仅供开发，生产请改强密码，并同步更新 `docker-compose.yml` 的 `POSTGRES_PASSWORD` 与 `.env` 的 `DATABASE_URL`。
- **后端热重载**：当前 `backend/Dockerfile` 的启动命令带 `--reload` 且 compose 挂载了源码目录（便于开发）。生产环境建议：
  - 去掉 `docker-compose.yml` 中 backend 的 `volumes` 挂载（`./backend:/app` 等）；
  - 把 Dockerfile 的 `CMD` 改为不带 `--reload`（如 `uvicorn app.main:app --host 0.0.0.0 --port 8001`）。
  - ⚠️ **别加 `--workers`**：并发闸门（见下条）是进程内信号量，多 worker 下每个进程各有一份，
    实际全局并发会变成 `LLM_MAX_CONCURRENCY × worker 数`，闸门形同虚设。真要多 worker，
    先把 `LLM_MAX_CONCURRENCY` 除以 worker 数，或把 `app/limits.py` 的 `LLMGate` 换成 Redis 实现。
    （另：多 worker 还要求 `JWT_SECRET` 必须显式配置，否则各 worker 密钥不同、登录直接不可用。）
- **并发与成本控制**（公网开放给同事用时必看）：`.env` 里有三层管控，默认值偏保守，按实际情况调：
  - `LLM_MAX_CONCURRENCY=3` — 全局同时在跑的大模型任务数，压的是**峰值**（服务器资源 + provider 侧限流）。
  - `LLM_MAX_CONCURRENCY_PER_USER=1` — 单账号同时最多一个任务，超了立刻 429。防一个人开多标签页霸占名额。
  - `DAILY_TOKEN_QUOTA=0` — 单账号每日 token 上限，**0 = 不限**。⚠️ 只有这层能真正封顶成本，
    并发只压峰值不压总量。建议先留 0 跑几天，用 `GET /api/limits/usage?granularity=day`（管理员）
    看真实分布再定值。参考量级：一次「30000 字 PRD → 澄清 + 生成」约 5~8 万 token。
  - 调参依据：`curl http://<host>:8001/health` 看 `gate.waiting`，长期 >0 说明卡在全局并发可以上调；
    provider 开始返 429/超时说明调过头了。
  - 用 OpenAI 兼容网关（火山方舟/DeepSeek 等）时保持 `LLM_STREAM_USAGE=true`，否则流式调用的
    token 统计不到、配额会漏算；个别网关不认 `stream_options` 参数会直接报错，那就置 false。
- **防火墙**：仅对外开放需要的端口。走 A.7/B.6 的统一 Nginx 时，只需放行 80/443，把 8001、5432 限制在本机。
- **LLM 超时**：文档较长、用思考型模型时，若出现「正在抽取产品知识…」一直转圈，调大 `.env` 的 `LLM_TIMEOUT_SECONDS`。
- **飞书文档导入**（可选功能）：依赖 `lark-cli`。Docker 部署下后端在容器里，配置较特殊，见下方「附录：飞书文档导入（lark-cli）配置」。不使用该功能可忽略。

## 4. 常见问题

| 现象 | 排查 |
|------|------|
| 前端页面能打开但所有请求失败/跨域报错 | `frontend/.env` 的 `VITE_API_URL` 是否为真实服务器地址；`.env` 的 `CORS_ORIGINS` 是否包含前端访问地址；改完前端需**重新构建** |
| 后端启动报 pgvector 相关错误 | 数据库是否为 pgvector 镜像 / 是否执行过 `CREATE EXTENSION vector` |
| 后端日志 `alembic upgrade failed` | 非致命（服务仍会起），但需关注；通常是数据库连接或历史 schema 问题，查 `journalctl` / `docker compose logs backend` |
| 上传大文档超时 | 调大 `LLM_TIMEOUT_SECONDS`；确认 Nginx `proxy_read_timeout` 足够（已给 300s） |
| SSE 流式（聊天/生成进度）不流式、一次性返回 | 反代必须 `proxy_buffering off`（本文的 Nginx 配置已包含） |
| `docker-compose up` 报 `KeyError: 'ContainerConfig'` | v1 与新版镜像不兼容的已知 bug。先 `docker-compose down` 再 `up -d`；或改用 v2 `docker compose`。见 A.1.2 |
| 启动报 `bind: address already in use`（5432/8001/3001） | 宿主机端口被占用（常见于同机已跑另一套项目）。db 可用 `.env` 的 `DB_HOST_PORT` 改宿主机端口；其他服务改 `docker-compose.yml` 里 `ports` 左侧的宿主机端口 |
| 拉基础镜像超时 / `failed to resolve reference "docker.io/..."` | 未配国内镜像加速器。见 A.1.1；ghcr.io 的 embedding/rerank 镜像另用 `.env` 的 `EMBEDDING_IMAGE` / `RERANK_IMAGE` 换源 |
| embedding 容器起了但一直无日志 / 语义检索始终降级 | 模型权重下载卡在 huggingface.co。`.env` 设 `HF_ENDPOINT=https://hf-mirror.com` 后重建 embedding、reranker 容器 |
| 装 Docker 时 `curl: (35) Recv failure` / `gpg: no valid OpenPGP data found` | 连不上 `download.docker.com`。两处 URL 换成 `https://mirrors.aliyun.com/docker-ce/linux/ubuntu`。见 A.1 |
| 改完 `daemon.json` 后 dockerd 起不来 | daemon.json 不是合法 JSON。`python3 -m json.tool` 校验，重写后 `systemctl reset-failed docker` 再 `start`。见 A.1.1 |
| 飞书导入报 `lark-cli 未安装` / `invalid_client` / `token_missing` | 见「附录：飞书文档导入（lark-cli）配置」 |

---

## 附录：飞书文档导入（lark-cli）配置

可选功能。允许粘贴飞书文档 URL（docx / wiki / docs）直接导入。依赖 [`lark-cli`](https://www.npmjs.com/package/@larksuite/cli)。**不用此功能可整节跳过。**

### 为什么 Docker 下要特殊处理

后端跑在容器里，而 `lark-cli` 是**宿主机上**的 Node 程序，且它的凭证不是普通文件——容器默认既找不到二进制、也读不到凭证。要点：

- **二进制**：`lark-cli` 依赖整个 Node 运行时，光拷二进制没用。做法是把宿主机的 Node 目录挂进容器。
- **身份**：`lark-cli` 支持 `user`（个人授权，`auth login`）和 `bot`（应用，app_id+secret）两种身份。
  - **user token 存进 OS keychain**，容器读不到，且会过期需重新浏览器授权——**不适合服务器/容器**。
  - **bot 凭证是 app_id+secret**，静态不过期，是服务器部署的正解。**本项目默认 `bot`**（`LARK_CLI_IDENTITY=bot`）。
- **凭证持久化**：`lark-cli config init` 把 App Secret 也存进**容器级 keychain**（`config.json` 里只留 `source=keychain` 指针），`docker compose down` 重建容器后即失效。为此后端镜像内置了 `docker-entrypoint.sh`，**每次启动用 `.env` 里的 secret 自动重新 `config init`，自愈**。

### 配置步骤（Docker 方式）

**1. 飞书开放平台准备**（[open.feishu.cn](https://open.feishu.cn/)）
- 创建/选用一个企业自建应用，记下 **App ID** 和 **App Secret**（凭证与基础信息页）。
- 权限管理里开通文档读取权限（至少 `docx:document:readonly`，导入知识库 wiki 链接还需 wiki 节点读取权限），**发布应用版本**才生效。
- 把要导入的文档 / 知识库**共享给该应用**（bot 是独立机器人，默认看不到你的文档）。

**2. 在宿主机安装 Node 与 lark-cli**（新服务器必做；机器上已有 lark-cli 可跳到第 3 步）

容器挂的是宿主机的 Node 目录，所以 Node 必须装在**宿主机**上，且要是一个自带 `bin/`、`lib/` 的完整目录（nvm 装出来的、或官方 tarball 解压出来的都符合）。用 apt / NodeSource 装的 Node 分散在 `/usr/bin` 与 `/usr/lib`，**不能**用于这里的挂载。

推荐直接解压官方 tarball（走阿里云镜像，避免 nvm 脚本从 GitHub 拉不动）：

```bash
uname -m          # x86_64 → 用下面的 x64 包；aarch64 → 把 x64 换成 arm64

cd /opt
curl -fsSLO https://mirrors.aliyun.com/nodejs-release/v22.11.0/node-v22.11.0-linux-x64.tar.xz
tar -xf node-v22.11.0-linux-x64.tar.xz
export PATH=/opt/node-v22.11.0-linux-x64/bin:$PATH    # 想长期生效就写进 ~/.bashrc
node -v

# 装 lark-cli（npm 换国内源）
npm config set registry https://registry.npmmirror.com
npm i -g @larksuite/cli
which lark-cli    # → /opt/node-v22.11.0-linux-x64/bin/lark-cli
```

**3. 把 Node 目录写进 `.env`**（`docker-compose.yml` 的挂载已配好，只需给对路径）

`LARK_NODE_DIR` 填 `which lark-cli` 结果**去掉末尾 `/bin/lark-cli`** 的那段：

```ini
LARK_NODE_DIR=/opt/node-v22.11.0-linux-x64        # 用 nvm 装的则形如 /root/.nvm/versions/node/v24.14.0
LARK_CLI_HOME=/root/.lark-cli                     # lark-cli 配置目录
```

**4. 填 bot 凭证到 `.env`**

`LARK_CLI_IDENTITY=bot` 不能漏——缺了会走 user 身份，在容器里必然失败（user token 存在 OS keychain，容器读不到）。

```ini
LARK_CLI_IDENTITY=bot
LARK_APP_ID=cli_xxxxxxxxxxxxxxxx
LARK_APP_SECRET=你的AppSecret
```

**5. 重建后端**（Dockerfile / 挂载变更，需 `--build`）
```bash
docker compose down && docker compose up -d --build
```

**6. 验证**
```bash
# 看 entrypoint 是否成功自动 init
docker compose logs backend | grep entrypoint
# 应出现：[entrypoint] lark-cli bot 凭证已初始化 (app_id=cli_...)

# 容器里实拉一篇文档（换成你的、且已共享给应用的文档 URL）
docker compose exec backend lark-cli docs +fetch --doc "https://xxx.feishu.cn/wiki/xxxx" --as bot --format json
```
返回 `"ok": true` + 内容即成功。之后网页端粘贴飞书链接导入即可。

### 排错

| 现象 | 原因 / 处理 |
|------|-------------|
| `lark-cli 未安装：No such file or directory` | 容器没挂到 Node。常见于新机器压根没装（见第 2 步），或 `.env` 的 `LARK_NODE_DIR` 指向了不存在的路径——**路径不存在时 Docker 会静默挂成空目录**，没有任何报错。用 `ls $LARK_NODE_DIR/bin/lark-cli` 确认宿主机侧存在，再 `docker compose exec backend lark-cli --version` 验证容器侧 |
| `invalid_client` / `The auth method is not supported` | bot 凭证没生效。看 `docker compose logs backend \| grep entrypoint`；确认 `.env` 的 `LARK_APP_ID/LARK_APP_SECRET` 已填、且容器已 `--build` 重建 |
| `token_missing` + `identity: user` | 后端仍走 user 身份。确认 `LARK_CLI_IDENTITY=bot` 且容器已重启读到新配置 |
| 抓取报无权限 / 读不到文档内容 | 应用权限没开全或没发布，或目标文档没共享给应用。回开放平台补权限、发版本、共享文档 |
