# Viora AI · AI 视频生成与商业运营平台

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
| 视频清晰度 | **1080p（默认） / 2K 超清**，2K 消耗 1080p 的 **2 倍**积分 |
| 5 秒视频 1080p | **10 积分** ／ 2K：**20 积分** |
| 10 秒视频 1080p | **20 积分** ／ 2K：**40 积分** |
| 其他时长 | 1080p 2 积分 / 秒线性（15s=30、20s=40、30s=60）；2K 全部 ×2 |

档位与倍率在 `backend/app/config.py` 的 `RESOLUTIONS` 里定义，加新档位（比如 4K）只需加一行，
`_upgrade_resolutions()` 会在下次启动时自动把价格表补齐。

套餐默认：体验包 10 元 / 100 积分、基础包 50 元 / 500 积分、标准包 200 元 / 2,200 积分、
专业包 500 元 / 5,750 积分、工作室包 1,000 元 / 12,000 积分、企业包 2,000 元 / 24,000 积分。
全部可在 `.env` 的 `RECHARGE_PACKAGES` 或后台「价格规则」里改。

### 充值赠送（三档）

| 单笔充值 | 赠送 |
|---|---|
| 不足 200 元 | 不赠 |
| 200 ~ 499 元 | 赠 **10%** |
| 500 ~ 999 元 | 赠 **15%** |
| 1000 元及以上 | 赠 **20%** |

* 规则写在 `config.py` 的 `BONUS_TIERS`，是**唯一来源**，套餐里不用手写 `bonus`；
  真要做活动想临时改，才在 `RECHARGE_PACKAGES` 里显式写 `bonus`，写了就以写的为准。
* 赠送按**充值本金**算：充 500 元 = 5,000 积分 + 750 赠送 = 到账 5,750。
* 全部整数运算（`bonus_for()`），不会出现 0.1+0.2 那种浮点误差。

### 自定义充值

钱包页除了固定套餐，还有「自定义金额」入口：

* 最低 **10 元**，最高 **20,000 元**（`CUSTOM_MIN_YUAN` / `CUSTOM_MAX_YUAN`）
* 输入即调 `GET /api/wallet/custom-quote?yuan=200` 实时试算「到账多少、送多少」
* 下单走 `POST /api/payments/checkout`，body 传 `{"custom_yuan": 200}`；金额只由服务端换算，
  前端传的任何积分/价格字段都不采信
* 赠送规则与套餐完全一致（走同一个 `bonus_for`）

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

打开 <http://127.0.0.1:8080>，用 `.env` 里的 `ADMIN_USERNAME / ADMIN_PASSWORD` 登录。

充值走**真实收款**：`.env` 里填好 `ALIPAY_APP_ID` + 两把密钥，重启后钱包页就能扫码付款，
钱进你的支付宝商户账户，回调到账后积分立刻入账。

> 本版本**不提供任何自动兜底**：没有商户参数时，钱包页直接显示「收款通道未配置」，
> 下单接口返回 503，不会静默生成假二维码。模拟支付只在非生产环境显式开启
> （`ALIPAY_MOCK=1`），且 `APP_ENV=prod` 时打开会被启动守卫拒绝启动。

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
- `desktop/dist/VioraAI.exe` —— 免安装版
- `desktop/installer_out/VioraAI-Setup.exe` —— 安装包

把安装包放到服务器 `releases/VioraAI-Setup.exe`，并在客户端 `config.json` 里把 `url` 改成你的域名，前端「桌面客户端」页就能直接下载 + 自动升级。

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

## 8. 注册与登录验证

注册要过**人机验证 + 邮箱验证码**，登录支持**密码登录**和**邮箱验证码登录**两套。

### 8.1 人机验证（极验 GeeTest v4）

默认走**极验滑块**，挡脚本批量注册和刷邮箱配额：

- 前端从 `https://static.geetest.com/v4/gt4.js` 加载组件，用户拖动滑块完成验证
- 前端只拿到 4 个参数（`lot_number` / `captcha_output` / `pass_token` / `gen_time`），
  **验证结论一律由服务端二次校验决定**，前端回调不算数
- 服务端用 `GEETEST_KEY` 对 `lot_number` 做 HMAC-SHA256 得到 `sign_token`，
  再连同 4 个参数 POST 到 `https://gcaptcha4.geetest.com/validate`，
  只有返回 `status=success` 且 `result=success` 才放行
- `GEETEST_KEY`（秘钥）只在服务端参与签名，**任何接口都不会把它下发给前端**；
  网页源码里只有 `GEETEST_ID`（验证 ID，本来就是公开的）

开关在 `.env`：

```bash
GEETEST_ID=          # 极验后台的验证 ID（公开信息）
GEETEST_KEY=         # 极验后台的验证秘钥（只留在服务端）
CAPTCHA_PROVIDER=auto   # auto=配了极验用极验 / geetest / image / off
```

`GEETEST_FAIL_OPEN=0`（默认）表示极验服务器不可达时**拒绝**注册而不是放行；
如果你的业务更怕「验证服务挂了没人能注册」，可以改 1，但要清楚这是拿安全性换可用性。

**兜底**：没配极验时自动回落到内置的自绘图形码（4 位字符、SHA-256 存哈希、
一次性、5 分钟过期、按 IP 限速），不需要改代码。也可以用 `CAPTCHA_PROVIDER=image`
强制走图形码（内网环境访问不到极验时有用）。

排查一句话：`curl /api/auth/captcha/config`，返回 `provider: "geetest"` 即表示极验生效；
返回 `image` 说明 ID/KEY 没读到。

### 8.2 邮箱验证码

| 场景 | 流程 |
|---|---|
| 注册 | 填邮箱 → 过极验 → 收 6 位邮箱码 → 带码注册 |
| 登录 | 切到「验证码登录」→ 填邮箱 → 过极验 → 收码 → 登录 |

- 6 位数字，只存哈希，有效期 10 分钟（`EMAIL_CODE_TTL`）
- 同一邮箱 **60 秒冷却**、**每小时最多 5 封**，单张码最多试 **5 次**
- 用一次即作废；换发新码会让旧码失效
- 注册时邮箱必须**没被占用**，登录时邮箱必须**已注册**，两种情况都不暴露多余信息
- 注册成功照旧送 `SIGNUP_BONUS`（默认 20 积分）

配 SMTP 才会真发信：

```bash
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的邮箱@qq.com
SMTP_PASS=授权码           # 不是登录密码
SMTP_FROM=                 # 留空用 SMTP_USER
SMTP_SSL=1                 # 465 走 SSL；587 则 SSL=0 STARTTLS=1
EMAIL_CODE_REQUIRED=1      # 设 0 则注册只校验人机验证
MAIL_ECHO=0                # 没配 SMTP 时把验证码回显到页面，线上必须 0
```

没配 SMTP 时**不会**自动把验证码回显出来，注册会明确报错「邮件服务还没配置」。
本地调试要跑通注册流程，才临时把 `MAIL_ECHO` 改成 1 —— 此时验证码会回显在页面上
（接口返回 `echo_code`），前端自动填入并弹提示。`APP_ENV=prod` 时打开 `MAIL_ECHO`
会被启动守卫直接拒绝启动，不存在「线上误开」这种事故。

接口一览：`GET /api/auth/captcha/config`（人机验证方式）、`GET /api/auth/captcha`（图形码兜底）、
`GET /api/auth/mail/config`、`POST /api/auth/email-code`、
`POST /api/auth/register`、`POST /api/auth/login`（`mode=password|code`）。

要换成短信验证码的话，把 `emailcode.issue/verify` 换成短信服务商的接口即可，
注册/登录接口的字段名 `email_code` 可以继续用。

---

## 9. 接入通道（支付 / 后端）

两条通道都预留好了，改 `.env` 即可，不用改代码、不用重打前端。

**支付通道**：`backend/app/payments/gateway.py` 是适配层，业务代码只认统一接口。

| 通道 | 状态 | 怎么开 |
|---|---|---|
| 支付宝当面付 | 已实现，可直接上线收真钱 | 配 `ALIPAY_APP_ID` + 两个密钥 + `ALIPAY_NOTIFY_URL` |
| 关闭充值 | 合法状态 | `PAYMENT_CHANNEL=none`，钱包页显示「通道未开通」 |
| 模拟支付 | **仅本地验收**，生产环境不注册 | `ALIPAY_MOCK=1`（`APP_ENV=prod` 时打开会拒绝启动） |
| 微信 / 易支付 / 手工转账 | 留了扩展位 | 新建一个 `BaseAdapter` 子类 + 注册表加一行 + `PAYMENT_CHANNEL=xxx` |

排查支付问题先看 `GET /api/payments/channels`（当前通道 + 每条通道是否就绪）。

**后端服务器通道**：前端后端地址收敛在 `core.js` 的 `API_BASE`，由后端 `GET /config.js` 下发。

- 同源部署：什么都不用配（默认）
- 前后端分离：后端配 `CORS_ORIGINS`，前端 `config.js` 里 `api_base` 填后端地址
- 子路径反代：`API_BASE=/viora` + Nginx `location /viora/`
- 第三方系统调用：`Authorization: Bearer <device-token>`，接口文档在 `/api/docs`

完整步骤（含密钥配置、回调三要求、联调验收、常见报错、加新通道的代码模板）见
**[`docs/接入说明.md`](docs/接入说明.md)**。
模拟能力关闭到什么程度、生产必填项、启动失败怎么对，见
**[`docs/上线检查.md`](docs/上线检查.md)**。
邮件发不出去 / 注册页提示「邮件服务还没配置」，见
**[`docs/邮件配置.md`](docs/邮件配置.md)**（QQ / 163 / 企业邮参数表 + 授权码怎么拿 + 报错对照），
配完跑 `cd backend && python tools/mailtest.py 你的邮箱@example.com` 自检。

---

## 10. 上线前自检（模拟能力已全部关闭）

这个版本把「模拟 / 演示」的自动兜底**全部删掉了**，改成一处代码级的启动闸门：
`backend/app/guard.py`。

### 10.1 三道闸

| 闸门 | 行为 |
|---|---|
| 生产环境开着模拟开关 | `ALIPAY_MOCK=1` / `MAIL_ECHO=1` / `PAYMENT_CHANNEL=mock` 任一命中 → **进程直接退出**，退出码 2，不会带病运行 |
| 模拟通道不注册 | `APP_ENV=prod` 时 `MockAdapter` 根本不进 `REGISTRY`，`/api/payments/mock-pay` 压根不存在（不是 404，是没这条路由） |
| 关键配置缺失 | 缺上游密钥 / 管理员密码 / 支付参数 / SMTP → 同样拒绝启动，避免「能打开但什么都干不了」 |

启动后被拒绝时，终端会直接列出还缺哪几项、每项该怎么改，不用猜。

### 10.2 生产环境必须齐的五项

| 配置 | 缺了会怎样 |
|---|---|
| `MUSE_KEY` | 所有生成任务失败 |
| `ADMIN_PASSWORD` | 拒绝启动（不再回落到内置默认口令） |
| `ALIPAY_APP_ID` / `ALIPAY_PRIVATE_KEY_PATH` / `ALIPAY_PUBLIC_KEY_PATH` | 用户点充值报错；不想开充值就写 `PAYMENT_CHANNEL=none` |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | 注册全挂（邮箱验证码是必经步骤）；不想用邮箱验证就设 `EMAIL_CODE_REQUIRED=0` |
| `PUBLIC_BASE`（https） | Cookie 不带 Secure，支付宝回调要求 https |

### 10.3 启动日志自查

```
运行状态：Viora AI · 环境 PROD · 支付宝当面付（真实收款） · SMTP 已配置
```

出现下面任一条，就是还没配好：

- `【模拟支付】不会真收钱` → 生产环境根本起不来，除非 `APP_ENV` 不是 prod
- `充值已关闭（...）` → 没有可用支付通道，或显式 `PAYMENT_CHANNEL=none`
- `【验证码回显】未配置 SMTP` → 验证码直接显示在页面上，等于没验证
- `SMTP 未配置，邮箱验证码将发不出去` → 注册会失败

### 10.4 想跑验收又不想真付钱

只能在**非生产环境**：`APP_ENV=dev` + `ALIPAY_MOCK=1`，此时钱包页会出现
「模拟支付」按钮，点一下直接到账。验收完把 `ALIPAY_MOCK` 改回 0。

---

## 11. 已知边界

- **号池不支持取消**：已提交给上游的任务不能取消，只有还在排队（`QUEUED`）的能取消退款。
- **Muse 实际固定渲染约 10 秒**：选 5 秒 / 15 秒只是提示词约束，不是硬切；按时长计价时注意这点。
- **单机 SQLite**：设计上限约「几十万任务、日均千单」量级。要横向扩就得换 PostgreSQL + 外部队列，`tasks.py` / `db.py` 是唯一需要动的地方。
- **媒体本地存储**：成品落在 `data/media/`。要做多机或 CDN，把 `signing.media_url` 换成对象存储签名地址即可。
- **4 位图形码挡不住打码平台**：能拦住脚本批量注册，挡不住花钱人工打码。要更强就换短信验证码（注册接口字段已预留）。
