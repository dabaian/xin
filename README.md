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

账号体系是 **手机号 + 密码**，验证码走**短信**：

| 场景 | 流程 |
|---|---|
| 注册 | 填手机号 → 过极验滑块 → 收 6 位短信码 → 设密码 → 完成并自动登录 |
| 登录 | 手机号 + 密码 |
| 找回密码 | 填手机号 → 过极验滑块 → 收 6 位短信码 → 设新密码 → 用新密码登录 |

注册成功照旧送 `SIGNUP_BONUS`（默认 20 积分）。密码重置后，该账号在所有设备上的
旧会话会立即失效。

### 8.1 人机验证（极验 GeeTest v4）

默认走**极验滑块**，挡脚本批量注册和刷短信配额（短信是真金白银，这一层不能省）：

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

### 8.2 短信验证码（短信宝）

通道是**短信宝**，官方文档 <https://www.smsbao.com/openapi/213.html>。
接口：`GET https://api.smsbao.com/sms?u=&p=&m=&c=`，返回纯文本，`0` 为成功。

- 6 位数字，只存 SHA-256 哈希，有效期 5 分钟（`SMS_CODE_TTL`）
- 同一手机号 **60 秒冷却**、**每小时最多 5 条**；同一 IP 每小时 20 条
- 单张码最多试 **5 次**，超过即作废；用一次即作废
- 注册时手机号必须**没被占用**，找回密码时手机号必须**已注册**

```bash
SMSBAO_USER=你的短信宝账号
SMSBAO_KEY=登录密码的32位MD5      # 不是明文密码！填明文会返回 30
SMSBAO_API=https://api.smsbao.com/sms
SMS_TEMPLATE=【晟众远网络科技】您正在注册Viora AI平台账号，您的验证码是{code}，如非本人操作，请忽略本短信！
SMS_TEMPLATE_RESET=【晟众远网络科技】您正在找回Viora AI平台账号密码，您的验证码是{code}，如非本人操作，请忽略本短信！
SMS_CODE_TTL=300
SMS_ECHO=0                        # 生产必须 0，写 1 会拒绝启动
```

**上线前必做**：到短信宝后台申请 **VIP 通道模板** 并联系客服报备。
没报备的话接口照样返回 `0`、照样扣费，但手机收不到 —— 这是最容易误判成「代码有问题」的坑。

配完先自检，别等到注册页试错：

```bash
cd backend
python tools/smstest.py                 # 体检：账号、密钥形态、余额、模板长度
python tools/smstest.py 13800138000     # 体检 + 真发一条（扣 1 条余额）
```

完整排错表见 [docs/短信配置.md](docs/短信配置.md)。

接口一览：`GET /api/auth/captcha/config`（人机验证方式）、`GET /api/auth/captcha`（图形码兜底）、
`GET /api/auth/sms/config`、`POST /api/auth/sms-code`、
`POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/reset-password`。

### 8.3 老账号兼容

数据库里仍保留 `email` 字段（`NOT NULL UNIQUE`）。手机号注册的账号写入
`138xxxxxxxx@mobile.invalid` 作为占位（`.invalid` 是 RFC 2606 保留域名，永远不是真实邮箱），
所以老用户的数据不动、也不用重建表。

登录接口的 `account` 字段依次按 **手机号 → 邮箱 → 用户名** 匹配，
所以之前用邮箱注册的老用户仍然可以「邮箱 + 密码」登录；只是找回密码只支持手机号，
没有绑定手机号的老账号需要管理员在后台重置密码。

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
短信收不到 / 返回码看不懂 / 模板要不要报备，见
**[`docs/短信配置.md`](docs/短信配置.md)**（接口参数、MD5 生成、返回码对照、排查清单），
配完跑 `cd backend && python tools/smstest.py 你的手机号` 自检。
（SMTP 已不参与注册，需要发通知邮件时看 **[`docs/邮件配置.md`](docs/邮件配置.md)**。）

---

## 10. 上线前自检（模拟能力已全部关闭）

这个版本把「模拟 / 演示」的自动兜底**全部删掉了**，改成一处代码级的启动闸门：
`backend/app/guard.py`。

### 10.1 三道闸

| 闸门 | 行为 |
|---|---|
| 生产环境开着模拟开关 | `ALIPAY_MOCK=1` / `MAIL_ECHO=1` / `SMS_ECHO=1` / `PAYMENT_CHANNEL=mock` 任一命中 → **进程直接退出**，退出码 2，不会带病运行 |
| 模拟通道不注册 | `APP_ENV=prod` 时 `MockAdapter` 根本不进 `REGISTRY`，`/api/payments/mock-pay` 压根不存在（不是 404，是没这条路由） |
| 关键配置缺失 | 缺管理员密码 / 短信宝账号 / 支付参数（未显式关闭时）→ 同样拒绝启动，避免「能打开但什么都干不了」 |

启动后被拒绝时，终端会直接列出还缺哪几项、每项该怎么改，不用猜。

### 10.2 生产环境必须齐的五项

| 配置 | 缺了会怎样 |
|---|---|
| `SMSBAO_USER` / `SMSBAO_KEY` | 注册和找回密码全挂（短信验证码是必经步骤）；生产环境会**拒绝启动** |
| `ADMIN_PASSWORD` | 拒绝启动（不再回落到内置默认口令） |
| `MUSE_KEY` | 页面能开，但所有生成任务失败。默认只告警；设 `MUSE_REQUIRED=1` 可改为拒绝启动 |
| `ALIPAY_APP_ID` / `ALIPAY_PRIVATE_KEY_PATH` / `ALIPAY_PUBLIC_KEY_PATH` | 用户点充值报错；不开放充值就写 `PAYMENT_CHANNEL=none` |
| `PUBLIC_BASE`（https） | Cookie 不带 Secure，账号会话在 https 站上会掉 |

### 10.3 启动日志自查

```
运行状态：Viora AI · 环境 PROD · 充值已关闭（PAYMENT_CHANNEL=none ...） · 短信宝已配置（账号）
人机验证：极验 v4（captcha_id 15db894a…）
短信通道：短信宝（账号 xxx，验证码 5 分钟有效）
```

出现下面任一条，就是还没配好：

- `【模拟支付】不会真收钱` → 生产环境根本起不来，除非 `APP_ENV` 不是 prod
- `充值已关闭（...）` → 没有可用支付通道，或显式 `PAYMENT_CHANNEL=none`
- `【验证码回显】未配置短信宝` → 验证码直接显示在页面上，等于没验证
- `短信宝未配置（SMSBAO_USER / SMSBAO_KEY），注册与找回密码都会失败` → 注册走不通
- `MUSE_KEY 未配置：页面能开，但所有生成任务都会失败` → 补上游密钥

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
