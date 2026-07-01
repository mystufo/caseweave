#!/usr/bin/env bash
# 服务器上 git pull 之后跑这一条：./deploy.sh
# 假设：服务器已装 python3.11、postgres 已起、.env 已配好。
set -euo pipefail

cd "$(dirname "$0")"

# 1) 确认 Python 3.11
if ! command -v python3.11 >/dev/null 2>&1; then
    echo "✗ 找不到 python3.11，请先安装（apt install python3.11-venv 或类似）"
    exit 1
fi

# 2) 拉最新代码
git pull --ff-only

# 3) 同步 venv —— 用 lock 文件保证版本一致
make install-prod

# 4) 前端构建（生产环境跑 build，不跑 dev server）
cd frontend
npm ci
npm run build
cd ..

# 5) 重启后端服务
#    根据你的进程管理器选一种：
#    - systemd:  sudo systemctl restart testcraft-backend
#    - pm2:      pm2 restart testcraft-backend
#    - 裸跑:     pkill -f "uvicorn app.main:app" || true
#                nohup backend/.venv/bin/uvicorn app.main:app \
#                    --host 0.0.0.0 --port 8001 \
#                    --app-dir backend > backend.log 2>&1 &
echo "✓ 同步完成。请按你的进程管理器手动重启后端服务。"
