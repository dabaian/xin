#!/usr/bin/env bash
# ==========================================================================
#  Viora AI 部署自检
#
#  用法：
#    bash deploy/verify.sh                     # 本机 127.0.0.1:8080
#    bash deploy/verify.sh your-domain.com     # 走域名（含 https）
#
#  只读检查，不改任何东西。全绿就说明部署是对的。
# ==========================================================================
set -uo pipefail

DOMAIN="${1:-}"
APP_DIR="${APP_DIR:-/opt/viora-ai}"

if [[ -n "$DOMAIN" ]]; then
  BASE="https://$DOMAIN"
else
  BASE="http://127.0.0.1:8080"
fi

PASS=0; FAIL=0; WARN=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠️  $1"; WARN=$((WARN+1)); }

echo "==> 检查目标: $BASE"
echo

# ---------------------------------------------------------------- 1. 服务
echo "[1/7] 服务进程"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet viora-ai; then ok "viora-ai 服务运行中"
  else bad "viora-ai 未运行 → systemctl status viora-ai --no-pager"; fi
else
  warn "非 systemd 环境，跳过"
fi
echo

# ---------------------------------------------------------------- 2. healthz
echo "[2/7] 健康检查"
HZ="$(curl -fsS --max-time 10 "$BASE/healthz" 2>/dev/null || true)"
if echo "$HZ" | grep -q '"ok":true'; then ok "/healthz → $HZ"
else bad "/healthz 无响应 → 检查 Nginx 与 8080 端口"; fi
echo

# ---------------------------------------------------------------- 3. readyz
echo "[3/7] 上游号池连通性"
RZ="$(curl -fsS --max-time 20 "$BASE/readyz" 2>/dev/null || true)"
if echo "$RZ" | grep -q '"ok":true'; then ok "/readyz 号池可达"
else
  bad "/readyz 失败 → 号池连不上，生成任务必然失败"
  echo "      返回: ${RZ:0:200}"
  echo "      排查: MUSE_BASE / MUSE_KEY 是否填对；号池服务是否在跑"
fi
echo

# ---------------------------------------------------------------- 4. 页面
echo "[4/7] 页面可访问"
for p in / /login /register /studio /tasks /wallet /library /download /admin; do
  CODE="$(curl -o /dev/null -s -w '%{http_code}' --max-time 10 "$BASE$p" 2>/dev/null || echo 000)"
  if [[ "$CODE" == "200" ]]; then ok "GET $p → 200"
  else bad "GET $p → $CODE"; fi
done
echo

# ---------------------------------------------------------------- 5. 配置
echo "[5/7] 关键配置"
ENVF="$APP_DIR/.env"
if [[ -f "$ENVF" ]]; then
  getv() { grep -E "^$1=" "$ENVF" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'"'"' ' ; }
  PB="$(getv PUBLIC_BASE)"
  MB="$(getv MUSE_BASE)"
  MK="$(getv MUSE_KEY)"
  AI="$(getv ALIPAY_APP_ID)"
  MOCK="$(getv ALIPAY_MOCK)"

  if [[ "$PB" == https://* ]]; then ok "PUBLIC_BASE = $PB"
  else bad "PUBLIC_BASE = '${PB}' → 必须是外网 https，否则图生视频取不到参考图"; fi

  if [[ -n "$MB" ]]; then ok "MUSE_BASE = $MB"; else bad "MUSE_BASE 为空"; fi
  if [[ -n "$MK" ]]; then ok "MUSE_KEY 已填（${#MK} 字符）"; else bad "MUSE_KEY 为空"; fi

  if [[ -n "$AI" ]]; then ok "ALIPAY_APP_ID = $AI"
  else warn "ALIPAY_APP_ID 未填 → 只能模拟支付"; fi

  AP="$(getv ADMIN_PASSWORD)"
  if [[ -z "$AP" ]]; then
    bad "ADMIN_PASSWORD 为空 → 正在使用内置默认密码，上线前必须改掉"
  elif [[ ${#AP} -lt 8 ]]; then
    warn "ADMIN_PASSWORD 只有 ${#AP} 位，建议 12 位以上"
  else ok "ADMIN_PASSWORD 已设置（${#AP} 位）"; fi

  if [[ "$MOCK" == "1" ]]; then
    if [[ -n "$AI" ]]; then bad "ALIPAY_MOCK=1 但已配支付宝 → 上线必须改成 0"
    else warn "ALIPAY_MOCK=1（模拟支付，未配支付宝时正常）"; fi
  else ok "ALIPAY_MOCK=0"; fi

  for k in alipay_private_key.pem alipay_public_key.pem; do
    if [[ -f "$APP_DIR/secrets/$k" ]]; then
      PERM="$(stat -c '%a' "$APP_DIR/secrets/$k" 2>/dev/null || echo '?')"
      if [[ "$PERM" == "600" ]]; then ok "secrets/$k 存在，权限 600"
      else warn "secrets/$k 权限是 $PERM，建议 chmod 600"; fi
    else warn "secrets/$k 不存在"; fi
  done
else
  bad "找不到 $APP_DIR/.env（如装在别处，用 APP_DIR=/your/path 重跑）"
fi
echo

# ---------------------------------------------------------------- 6. 目录权限
echo "[6/7] 数据目录"
for d in data data/media data/uploads releases logs; do
  if [[ -d "$APP_DIR/$d" ]]; then ok "$d 存在"
  else bad "$d 缺失 → mkdir -p $APP_DIR/$d && chown -R muse:muse $APP_DIR"; fi
done
if [[ -f "$APP_DIR/data/studio.db" ]]; then
  ok "数据库已初始化（$(du -h "$APP_DIR/data/studio.db" | cut -f1)）"
else
  warn "数据库未生成 → 首次访问 /healthz 后会自动建表"
fi
echo

# ---------------------------------------------------------------- 7. 日志
echo "[7/7] 最近日志（末 8 行）"
if [[ -f "$APP_DIR/logs/app.log" ]]; then
  if grep -qiE 'traceback|error' <(tail -n 200 "$APP_DIR/logs/app.log"); then
    warn "日志里有报错，末 8 行如下："
  else ok "日志无明显报错"; fi
  tail -n 8 "$APP_DIR/logs/app.log" | sed 's/^/      /'
else
  warn "日志文件不存在: $APP_DIR/logs/app.log"
fi

echo
echo "============================================================"
echo " 通过 $PASS 项 / 失败 $FAIL 项 / 提醒 $WARN 项"
if [[ "$FAIL" -eq 0 ]]; then
  echo " 结论：可以上线。"
else
  echo " 结论：先把 ❌ 项修掉再用。"
fi
echo "============================================================"
exit $(( FAIL > 0 ? 1 : 0 ))
