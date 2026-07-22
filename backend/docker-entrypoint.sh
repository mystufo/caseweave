#!/bin/sh
# 容器启动时自动初始化 lark-cli 的 bot 凭证。
#
# 为什么需要：lark-cli config init 把 App Secret 存进容器级 keychain（config.json 里只留
# 一个 source=keychain 的指针），keychain 绑定容器实例，docker-compose down 重建后即失效，
# 导致飞书导入报 invalid_client。这里在每次启动时用环境变量里的 secret 重新 init，自愈。
#
# 需要 .env 提供 LARK_APP_ID / LARK_APP_SECRET；缺失则跳过（不影响不使用飞书导入的部署）。
set -e

if [ -n "$LARK_APP_ID" ] && [ -n "$LARK_APP_SECRET" ]; then
  if printf '%s' "$LARK_APP_SECRET" | lark-cli config init --app-id "$LARK_APP_ID" --app-secret-stdin >/dev/null 2>&1; then
    echo "[entrypoint] lark-cli bot 凭证已初始化 (app_id=$LARK_APP_ID)"
  else
    echo "[entrypoint] 警告：lark-cli config init 失败，飞书导入可能不可用"
  fi
else
  echo "[entrypoint] 未设置 LARK_APP_ID/LARK_APP_SECRET，跳过 lark-cli 初始化"
fi

exec "$@"
