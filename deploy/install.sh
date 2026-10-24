#!/usr/bin/env bash
# ==========================================================================
#  Muse Studio 一键部署（Ubuntu 20.04+ / Debian 11+ / CentOS Stream 9）
#
#  用法：
#    sudo bash deploy/install.sh your-domain.com
#
#  做的事：
#    1. 建运行用户 + 目录（/opt/muse-studio）
#    2. Python venv + 依赖
#    3. systemd 服务（127.0.0.1:8080）
#    4. Nginx 反代（含 SSE / 大文件 / 长任务超时）
#    5. 生成 .env（缺的字段会提示你补）
#    6. 可选：certbot 申请证书
# ==========================================================================
set -euo pipefail

DOMAIN="${1:-}"
APP_USER="muse"
APP_DIR="/opt/muse-studio"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY_BIN="${PY_BIN:-python3}"

if [[ -z "$DOMAIN" ]]; then
  echo "用法: sudo bash deploy/install.sh <你的域名>"
  echo "例:   sudo bash deploy/install.sh studio.example.com"
  exit 1
fi

echo "==> 目标域名: $DOMAIN"
echo "==> 源码目录: $SRC_DIR"
echo "==> 安装目录: $APP_DIR"

# ---------------------------------------------------------------- 依赖
if command -v apt-get >/dev/null 2>&1; then
  PKG="apt-get"
  $PKG update -qq
  $PKG install -y -qq python3 python3-venv python3-pip nginx curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  PKG="dnf"
  $PKG install -y -q python3 python3-pip nginx curl ca-certificates
else
  echo "未识别的包管理器，请手动安装 python3 / nginx 后重试"
  exit 1
fi
echo "==> 依赖已就绪"

# ---------------------------------------------------------------- 用户
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER" || \
    useradd --system --create-home --shell /sbin/nologin "$APP_USER"
  echo "==> 已创建用户 $APP_USER"
fi

# ---------------------------------------------------------------- 目录
mkdir -p "$APP_DIR" "$APP_DIR/data" "$APP_DIR/releases" "$APP_DIR/secrets" "$APP_DIR/logs"
mkdir -p "$APP_DIR/backend" "$APP_DIR/frontend"
cat > "$APP_DIR/secrets/README.md" <<'SEC'
# 支付宝密钥放这里

| 文件名 | 是什么 | 从哪来 |
|---|---|---|
| alipay_private_key.pem | 应用私钥 | 自己在开放平台工具生成，只有你有 |
| alipay_public_key.pem  | 支付宝公钥 | 从开放平台「接口加签方式」页面复制，不是应用公钥 |

    chmod 600 /opt/muse-studio/secrets/*.pem

然后 .env 里填 ALIPAY_APP_ID / ALIPAY_PRIVATE_KEY_PATH / ALIPAY_PUBLIC_KEY_PATH，
并把 ALIPAY_MOCK 改成 0。填成「应用公钥」的症状是：付了钱、余额不加。
SEC
rsync -a --delete "$SRC_DIR/backend/" "$APP_DIR/backend/" 2>/dev/null || {
  cp -r "$SRC_DIR/backend/." "$APP_DIR/backend/"; }
rsync -a --delete "$SRC_DIR/frontend/" "$APP_DIR/frontend/" 2>/dev/null || {
  cp -r "$SRC_DIR/frontend/." "$APP_DIR/frontend/"; }
for f in .env.example README.md docs deploy; do
  if [[ -e "$SRC_DIR/$f" ]]; then cp -r "$SRC_DIR/$f" "$APP_DIR/"; fi
done

# ---------------------------------------------------------------- .env
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  sed -i "s#^PUBLIC_BASE=.*#PUBLIC_BASE=https://$DOMAIN#" "$APP_DIR/.env"
  sed -i "s#^ALIPAY_NOTIFY_URL=.*#ALIPAY_NOTIFY_URL=https://$DOMAIN/api/payments/notify#" "$APP_DIR/.env"
  sed -i "s#^ALIPAY_RETURN_URL=.*#ALIPAY_RETURN_URL=https://$DOMAIN/wallet#" "$APP_DIR/.env"
  ADMIN_PWD="$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 14)"
  sed -i "s#^ADMIN_PASSWORD=.*#ADMIN_PASSWORD=$ADMIN_PWD#" "$APP_DIR/.env"
  # 还没配支付宝密钥时，先开模拟支付，否则用户一分积分都充不进去、整个站点没法自测
  if grep -q '^ALIPAY_APP_ID=$' "$APP_DIR/.env"; then
    sed -i "s#^ALIPAY_MOCK=.*#ALIPAY_MOCK=1#" "$APP_DIR/.env"
    MOCK_ON=1
  else
    MOCK_ON=0
  fi
  chmod 600 "$APP_DIR/.env"
  echo "==> 已生成 .env，管理员密码：$ADMIN_PWD"
  echo "    ⚠️ 请立即记录，并补齐 MUSE_KEY / 支付宝相关配置"
  if [[ "$MOCK_ON" == "1" ]]; then
    echo "    ⚠️ 当前 ALIPAY_MOCK=1（模拟支付），配好支付宝密钥后务必改成 0"
  fi
else
  echo "==> 沿用已有 .env（未覆盖）"
  ADMIN_PWD="（见 $APP_DIR/.env）"
  MOCK_ON="$(grep -c '^ALIPAY_MOCK=1' "$APP_DIR/.env" || true)"
fi

# ---------------------------------------------------------------- venv
if [[ ! -d "$APP_DIR/venv" ]]; then
  "$PY_BIN" -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/backend/requirements.txt"
echo "==> Python 依赖安装完成"

chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ---------------------------------------------------------------- systemd
cat > /etc/systemd/system/muse-studio.service <<UNIT
[Unit]
Description=Muse Studio (AI Video Platform)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$APP_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
LimitNOFILE=65535
StandardOutput=append:$APP_DIR/logs/app.log
StandardError=append:$APP_DIR/logs/app.log

# 基本加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now muse-studio
echo "==> systemd 服务已启动：systemctl status muse-studio"

# ---------------------------------------------------------------- nginx
cat > /etc/nginx/conf.d/muse-studio.conf <<NGINX
server {
    listen 80;
    server_name $DOMAIN;

    client_max_body_size 20m;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;

    access_log /var/log/nginx/muse-studio.access.log;
    error_log  /var/log/nginx/muse-studio.error.log;

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;

    location /assets/ {
        proxy_pass http://127.0.0.1:8080;
        expires 7d;
        add_header Cache-Control "public, max-age=604800";
    }

    # 支付回调：不缓存、不缓冲、原样透传
    location = /api/payments/notify {
        proxy_pass http://127.0.0.1:8080;
        proxy_buffering off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_request_buffering off;
    }
}
NGINX

nginx -t && systemctl reload nginx
echo "==> Nginx 已配置并重载"

# ---------------------------------------------------------------- 证书
if command -v certbot >/dev/null 2>&1; then
  echo "==> 检测到 certbot，尝试签发证书"
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" || \
    echo "    ⚠️ 自动签发失败，请手动执行: certbot --nginx -d $DOMAIN"
else
  echo "==> 未安装 certbot。启用 HTTPS 请执行："
  echo "    apt-get install -y certbot python3-certbot-nginx"
  echo "    certbot --nginx -d $DOMAIN"
fi

cat <<TIP

============================================================
 部署完成
------------------------------------------------------------
 站点        : http://$DOMAIN
 后台        : http://$DOMAIN/admin
 管理员      : admin / $ADMIN_PWD
 配置文件    : $APP_DIR/.env
 日志        : $APP_DIR/logs/app.log
 重启        : systemctl restart muse-studio
 自检        : bash $APP_DIR/deploy/verify.sh $DOMAIN

 还需要你补齐 3 件事（改完记得 restart）：
   1. MUSE_BASE / MUSE_KEY  → 指向你的 muse2api 号池
   2. ALIPAY_APP_ID + 两个密钥文件 → 放到 $APP_DIR/secrets/
   3. PUBLIC_BASE 必须是外网可访问的 https 域名（图生视频要用）

 支付宝密钥放好后，务必把 .env 里的 ALIPAY_MOCK 改成 0，
 否则任何人都能点「模拟支付」白拿积分。
============================================================

TIP
