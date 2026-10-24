#!/usr/bin/env bash
# ==========================================================================
#  Viora AI 增量更新（代码已改，只推代码 + 重启，不动 .env / data）
#
#  用法：
#    sudo bash deploy/update.sh                # 从当前目录推代码
#    sudo bash deploy/update.sh /tmp/new-code  # 从指定目录推代码
#
#  会做：备份数据库 → rsync backend/frontend → 补装依赖 → 重启 → 健康检查
#  不会做：覆盖 .env、删除 data/、改 Nginx
# ==========================================================================
set -euo pipefail

SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_DIR="${APP_DIR:-/opt/viora-ai}"
APP_USER="${APP_USER:-muse}"
STAMP="$(date +%Y%m%d-%H%M%S)"

echo "==> 源目录: $SRC_DIR"
echo "==> 安装目录: $APP_DIR"

if [[ ! -d "$APP_DIR" ]]; then
  echo "❌ $APP_DIR 不存在，请先跑 deploy/install.sh"; exit 1
fi
if [[ ! -f "$SRC_DIR/backend/app/main.py" ]]; then
  echo "❌ $SRC_DIR 看起来不是项目根目录（缺 backend/app/main.py）"; exit 1
fi

# ---------------------------------------------------------------- 备份
mkdir -p "$APP_DIR/backups"
if [[ -f "$APP_DIR/data/studio.db" ]]; then
  # SQLite 在线备份（WAL 模式下直接 cp 可能不一致）
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$APP_DIR/data/studio.db" ".backup '$APP_DIR/backups/studio-$STAMP.db'"
  else
    cp "$APP_DIR/data/studio.db" "$APP_DIR/backups/studio-$STAMP.db"
  fi
  echo "==> 数据库已备份: backups/studio-$STAMP.db"
  # 只留最近 10 份
  ls -1t "$APP_DIR"/backups/studio-*.db 2>/dev/null | tail -n +11 | xargs -r rm -f
fi

# ---------------------------------------------------------------- 停服
systemctl stop viora-ai || true

# ---------------------------------------------------------------- 推代码
rsync -a --delete --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC_DIR/backend/" "$APP_DIR/backend/"
rsync -a --delete "$SRC_DIR/frontend/" "$APP_DIR/frontend/"
for f in docs deploy README.md; do
  if [[ -e "$SRC_DIR/$f" ]]; then cp -r "$SRC_DIR/$f" "$APP_DIR/"; fi
done
echo "==> 代码已同步"

# ---------------------------------------------------------------- 依赖
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"
echo "==> 依赖已更新"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------- 起服
systemctl start viora-ai
sleep 3

if curl -fsS --max-time 10 http://127.0.0.1:8080/healthz | grep -q '"ok":true'; then
  echo "==> ✅ 更新完成，服务健康"
  echo "    如需回滚：cp $APP_DIR/backups/studio-$STAMP.db $APP_DIR/data/studio.db && systemctl restart viora-ai"
else
  echo "==> ❌ 服务未健康，看日志："
  tail -n 30 "$APP_DIR/logs/app.log" || true
  echo "    回滚数据库：cp $APP_DIR/backups/studio-$STAMP.db $APP_DIR/data/studio.db"
  exit 1
fi
