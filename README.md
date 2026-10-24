# Muse Studio · AI 视频生成与商业运营平台

按 `AI视频生成平台_完整开发架构与逐步实施方案_V3` 重写的完整实现：
**账号 → 充值 → 提交生成 → 自动结算 → 预览下载 → 后台运营** 全链路闭环。

- 上号调用链路复用现有 `muse2api` 号池网关（无需改动上游）
- 前端 UI、桌面 EXE、支付、账本、任务引擎全部重写
- 单机部署：FastAPI + SQLite(WAL) + 内置调度器，**无 Redis / 无 Celery / 无 Node 构建**

---

## 1. 定价

| 项目 | 价格 |
|---|---|
| 充值比例 | **1 元 = 10 积分** |
| 图片 | **5 积分 / 张** |
| 5 秒视频 | **10 积分** |
| 10 秒视频 | **20 积分** |
| 其他时长 | 2 积分 / 秒线性（15s=30、20s=40、30s=60） |

套餐默认：体验包 100 积分/10 元、基础包 500+50/50 元、专业包 1200+200/120 元、工作室包 3000+800/300 元。
全部可在 `.env` 的 `RECHARGE_PACKAGES` 或后台「价格规则」里改。

> 计费原则：**提交先冻结，成功才扣费，失败原路退回**。上游超时不等于失败，只查单不重提。

---

## 2. 目录

```
yice-muse/
├── backend/                FastAPI 服务
│   └── app/
│       ├── main.py         入口、路由挂载、页面分发
│       ├── config.py       全部配置（读 .env）
│       ├── db.py           SQLite 建表 / 迁移 / 审计
│       ├── security.py     scrypt 密码、会话令牌、限流
│       ├── ledger.py       积分账本（只追加流水，余额不得为负）
│       ├── tasks.py        任务状态机 + 轮询 + 结算
│       ├── signing.py      成品短时效签名链接
│       ├── providers/      供应商适配器（base 契约 + muse 实现）
│       ├── payments/       支付宝 RSA2 签名 / 验签 / 二维码
│       └── routers/        auth / tasks / assets / wallet / payment / admin / desktop
├── frontend/               纯静态 SPA（无构建）
│   ├── index.html          落地页
│   ├── login.html  register.html
│   ├── studio.html         生成工作台（视频 / 图片）
│   ├── tasks.html          任务中心
│   ├── wallet.html         钱包 + 支付宝扫码充值
│   ├── assets.html         素材库
│   ├── admin.html          运营后台
│   ├── download.html       客户端下载
│   └── assets/             app.css + core.js + favicon
├── desktop/                Windows 客户端（pywebview 原生窗口）
├── deploy/                 install.sh / nginx.conf / Dockerfile / compose
├── docs/                   部署指南 / 支付宝接入 / 验收清单
└── .env.example            配置模板
```

---

## 3. 本地跑起来（3 分钟）

```bash
cd yice-muse
cp .env.example .env          # Windows: copy .env.example .env

cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

打开 <http://127.0.0.1:8080>，用 `.env` 里的 `ADMIN_USERNAME / ADMIN_PASSWORD` 登录（默认 `admin` / `admin888`）。

没有支付宝密钥时，先设 `ALIPAY_MOCK=1` 把充值流程跑通：
选套餐 → 弹二维码 → 点「模拟支付（测试模式）」→ 余额立刻到账。

Windows 也可以直接双击 `start.bat`。

---

## 4. 上服务器

一键脚本（推荐，Ubuntu/Debian/CentOS）：

```bash
# 上传整个 yice-muse 目录到服务器，然后
sudo bash deploy/install.sh your-domain.com
```

脚本会建用户、装依赖、配 systemd、配 Nginx、生成 `.env` 和随机管理员密码，并尝试签 HTTPS 证书。

之后只要补三件事：

| 变量 | 说明 |
|---|---|
| `MUSE_BASE` / `MUSE_KEY` | 指向你的 muse2api 号池（同机就填 `http://127.0.0.1:18610`） |
| `ALIPAY_APP_ID` + 两个密钥 | 放到 `secrets/` 目录，权限 600 |
| `PUBLIC_BASE` | 必须是对外可访问的 https 域名（图生视频要把参考图给号池取） |

细节见 [docs/部署指南.md](docs/部署指南.md) 与 [docs/支付宝接入.md](docs/支付宝接入.md)。

---

## 5. 上游契约（复用现有号池，不用改）

```
POST /v1/videos/generations   {prompt, model, duration, aspect_ratio, resolution,
                               image_url, thread_key, reuse_thread, extra, timeout}
POST /v1/images/tasks         {prompt, model:muse-image, size, reference_image}
GET  /v1/videos/generations/{id}
GET  /v1/images/tasks/{id}
GET  /v1/media/{name}
GET  /healthz
Header: Authorization: Bearer <MUSE_KEY>
```

任务状态映射：`queued/processing → SUBMITTED/RUNNING`，`completed → SUCCEEDED`，`failed → FAILED`。

安全边界：
- 上游 task_id 一旦拿到就**只查单不重提**，避免重复收费；
- 上游说完成但没有成品 → 转 `NEEDS_REVIEW`（待对账），不自动扣费；
- 期望视频却拿到图片 → 拒绝交付 + 退款。

---

## 6. 桌面客户端

```bat
cd desktop
build.bat        :: 需要 python + pywebview；装了 Inno Setup 会自动出安装包
```

产物：
- `desktop/dist/MuseStudio.exe` —— 免安装版
- `desktop/installer_out/MuseStudio-Setup.exe` —— 安装包

把安装包放到服务器 `releases/MuseStudio-Setup.exe`，并在客户端 `config.json` 里把 `url` 改成你的域名，前端「桌面客户端」页就能直接下载 + 自动升级。

客户端能力：原生窗口（无浏览器壳）、记住登录、成品直接存到本地指定目录、静默自动升级、单实例。

---

## 7. 验收自测

```bash
# 健康检查
curl http://127.0.0.1:8080/healthz
curl http://127.0.0.1:8080/readyz     # 会顺带探号池

# 支付宝配置自检（登录后）
curl -b cookie.txt http://127.0.0.1:8080/api/payments/selfcheck
```

必测清单见 [docs/验收清单.md](docs/验收清单.md)。

---

## 8. 已知边界

- **号池不支持取消**：已提交给上游的任务不能取消，只有还在排队（`QUEUED`）的能取消退款。
- **Muse 实际固定渲染约 10 秒**：选 5 秒 / 15 秒只是提示词约束，不是硬切；按时长计价时注意这点。
- **单机 SQLite**：设计上限约「几十万任务、日均千单」量级。要横向扩就得换 PostgreSQL + 外部队列，`tasks.py` / `db.py` 是唯一需要动的地方。
- **媒体本地存储**：成品落在 `data/media/`。要做多机或 CDN，把 `signing.media_url` 换成对象存储签名地址即可。
