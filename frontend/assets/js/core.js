/* Viora AI · 前端基础库（无构建、无外部依赖） */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ---------------------------------------------------------------- 后端地址
 * 三处任选其一即可切换后端（优先级从上到下）：
 *   1. <script src="/config.js"></script>  由后端动态下发（推荐，改 .env 就生效）
 *   2. window.VIORA_API_BASE = 'https://api.xxx.com'  写在 core.js 之前
 *   3. <meta name="viora-api-base" content="https://api.xxx.com">
 * 都留空 = 同源部署，请求 /api/... 走当前域名。
 * 前后端分开部署时后端要配 CORS_ORIGINS，否则浏览器会拦跨域请求。
 */
const API_BASE = (() => {
  const cfg = (typeof window !== 'undefined' && window.VIORA_CONFIG) || {};
  const meta = document.querySelector && document.querySelector('meta[name="viora-api-base"]');
  const raw = cfg.api_base || (typeof window !== 'undefined' && window.VIORA_API_BASE)
    || (meta && meta.content) || '';
  return String(raw).trim().replace(/\/+$/, '');
})();

/* 把 /api/xxx 这类相对路径拼到后端地址上；已经是绝对地址的原样返回 */
function apiUrl(path) {
  if (!path) return path;
  if (/^(?:https?:)?\/\//i.test(path)) return path;
  if (/^(?:data|blob):/i.test(path)) return path;
  if (!API_BASE) return path;
  return API_BASE + (path.charAt(0) === '/' ? path : '/' + path);
}

/* 媒体/图片/视频地址同样要走 apiUrl，语义化的别名 */
const mediaUrl = apiUrl;

/* ---------------------------------------------------------------- 请求 */
async function api(path, opts = {}) {
  const init = {
    credentials: API_BASE ? 'include' : 'same-origin',
    headers: {},
    ...opts,
  };
  if (init.body && typeof init.body !== 'string' && !(init.body instanceof FormData)) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(init.body);
  }
  const res = await fetch(apiUrl(path), init);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
  if (!res.ok) {
    const err = new Error((data && (data.detail || data.message)) || ('请求失败 ' + res.status));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}
const get = (p) => api(p);
const post = (p, body) => api(p, { method: 'POST', body: body || {} });
const patch = (p, body) => api(p, { method: 'PATCH', body: body || {} });
const del = (p) => api(p, { method: 'DELETE' });

/* ---------------------------------------------------------------- Toast */
function toast(msg, type = '') {
  let box = $('#toasts');
  if (!box) { box = document.createElement('div'); box.id = 'toasts'; document.body.appendChild(box); }
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateY(-8px)'; t.style.transition = 'all .2s'; }, 2400);
  setTimeout(() => t.remove(), 2700);
}

/* ---------------------------------------------------------------- 弹窗 */
function modal({ title = '', body = '', footer = '', wide = false, onClose = null }) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal ${wide ? 'modal-lg' : ''}" role="dialog">
      <div class="modal-head"><div class="h3">${title}</div>
        <button class="modal-close" aria-label="关闭">&times;</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-body" style="padding-top:0">${footer}</div>` : ''}
    </div>`;
  const close = () => { mask.remove(); document.body.style.overflow = ''; onClose && onClose(); };
  mask.addEventListener('click', (e) => { if (e.target === mask) close(); });
  mask.querySelector('.modal-close').addEventListener('click', close);
  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
  });
  document.body.appendChild(mask);
  document.body.style.overflow = 'hidden';
  return { el: mask.querySelector('.modal'), close };
}

function confirmBox(title, message, okText = '确认', danger = false) {
  return new Promise((resolve) => {
    const m = modal({
      title,
      body: `<p class="sub" style="margin:0">${message}</p>`,
      footer: `<div class="row" style="justify-content:flex-end">
        <button class="btn" data-no>取消</button>
        <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-yes>${okText}</button>
      </div>`,
      onClose: () => resolve(false),
    });
    m.el.querySelector('[data-no]').onclick = () => { m.close(); resolve(false); };
    m.el.querySelector('[data-yes]').onclick = () => { m.close(); resolve(true); };
  });
}

/* ---------------------------------------------------------------- 格式化 */
const fmt = {
  points: (n) => (n == null ? '—' : Number(n).toLocaleString('zh-CN')),
  money: (minor) => '¥' + (Number(minor || 0) / 100).toFixed(2),
  yuan: (v) => '¥' + Number(v || 0).toFixed(2),
  bytes(n) {
    if (!n) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB'];
    let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(v >= 10 || i === 0 ? 0 : 1) + ' ' + u[i];
  },
  time(s) {
    if (!s) return '—';
    return String(s).replace('T', ' ').slice(0, 16);
  },
  ago(s) {
    if (!s) return '';
    const t = new Date(String(s).replace(' ', 'T')).getTime();
    if (isNaN(t)) return s;
    const d = (Date.now() - t) / 1000;
    if (d < 60) return '刚刚';
    if (d < 3600) return Math.floor(d / 60) + ' 分钟前';
    if (d < 86400) return Math.floor(d / 3600) + ' 小时前';
    if (d < 86400 * 30) return Math.floor(d / 86400) + ' 天前';
    return fmt.time(s);
  },
  dur(fromTime) {
    const t = new Date(String(fromTime).replace(' ', 'T')).getTime();
    if (isNaN(t)) return '';
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  },
};

const STATUS = {
  CREATED:    { text: '已创建', cls: 'tag',      pct: 2 },
  RESERVED:   { text: '已扣额度', cls: 'tag',    pct: 4 },
  QUEUED:     { text: '排队中', cls: 'tag-warn', pct: 8 },
  SUBMITTING: { text: '提交中', cls: 'tag-run',  pct: 12 },
  SUBMITTED:  { text: '已受理', cls: 'tag-run',  pct: 20 },
  RUNNING:    { text: '生成中', cls: 'tag-run',  pct: 55 },
  SUCCEEDED:  { text: '已完成', cls: 'tag-ok',   pct: 100 },
  FAILED:     { text: '失败', cls: 'tag-bad',    pct: 100 },
  CANCELLED:  { text: '已取消', cls: 'tag',      pct: 100 },
  NEEDS_REVIEW: { text: '待对账', cls: 'tag-warn', pct: 100 },
};
function statusTag(s) {
  // 兼容历史数据里的细分错误码（如 SUBMIT_FAILED / POLL_FAILED）
  const key = STATUS[s] ? s : (/FAIL/i.test(s || '') ? 'FAILED' : (/CANCEL/i.test(s || '') ? 'CANCELLED' : s));
  const m = STATUS[key] || { text: s, cls: 'tag' };
  const live = ['QUEUED', 'SUBMITTING', 'SUBMITTED', 'RUNNING'].includes(s);
  return `<span class="tag ${m.cls}">${live ? '<i class="dot"></i>' : ''}${m.text}</span>`;
}

/* ---------------------------------------------------------------- 图标 */
const ICON = {
  studio: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.5 20 8v8l-8 4.5L4 16V8z"/><path d="m12 12 8-4M12 12v8.5M12 12 4 8"/></svg>',
  tasks: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="m8 11 2.2 2.2L16 7.8"/></svg>',
  wallet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="13" rx="3"/><path d="M3 10h18"/><circle cx="16.5" cy="14.5" r="1.2"/></svg>',
  library: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="4.5" width="7" height="7" rx="2"/><rect x="13.5" y="4.5" width="7" height="7" rx="2"/><rect x="3.5" y="14.5" width="7" height="5" rx="2"/><rect x="13.5" y="14.5" width="7" height="5" rx="2"/></svg>',
  admin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3.2 19 6v5.4c0 4.3-2.9 7.6-7 9.4-4.1-1.8-7-5.1-7-9.4V6z"/><path d="m9.2 12 2 2 3.6-3.8"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v10m0 0 3.5-3.5M12 14l-3.5-3.5"/><path d="M4.5 17.5v1A2.5 2.5 0 0 0 7 21h10a2.5 2.5 0 0 0 2.5-2.5v-1"/></svg>',
  logout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 4.5h2.5A2.5 2.5 0 0 1 20 7v10a2.5 2.5 0 0 1-2.5 2.5H15"/><path d="M10 8.5 13.5 12 10 15.5M13.5 12H4"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4m0 10v4M3 12h4m10 0h4M5.6 5.6l2.8 2.8m7.2 7.2 2.8 2.8m0-12.8-2.8 2.8m-7.2 7.2-2.8 2.8"/></svg>',
  image: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="4.5" width="17" height="15" rx="3"/><circle cx="9" cy="10" r="1.6"/><path d="m4.5 17 4.2-4.2a2 2 0 0 1 2.8 0L16 17.5"/></svg>',
};

/* ---------------------------------------------------------------- 布局 */
const NAV = [
  { key: 'studio', href: '/studio', name: '生成工作台', icon: 'studio', group: '创作' },
  { key: 'tasks', href: '/tasks', name: '任务中心', icon: 'tasks', group: '创作' },
  { key: 'library', href: '/library', name: '素材库', icon: 'library', group: '创作' },
  { key: 'wallet', href: '/wallet', name: '钱包充值', icon: 'wallet', group: '账户' },
  { key: 'download', href: '/download', name: '桌面客户端', icon: 'download', group: '账户' },
];

function buildLayout(active, user, title) {
  const item = (n) => `
    <a class="nav-item ${n.key === active ? 'active' : ''}" href="${n.href}">
      ${ICON[n.icon]}<span>${n.name}</span>
    </a>`;
  const createNav = NAV.filter((n) => n.group === '创作').map(item).join('');
  const accountNav = NAV.filter((n) => n.group === '账户').map(item).join('');
  const adminNav = user && user.role === 'admin'
    ? `<div class="nav-group">运营</div>
       <a class="nav-item ${active === 'admin' ? 'active' : ''}" href="/admin">${ICON.admin}<span>管理后台</span></a>`
    : '';
  const initial = (user?.username || user?.email || 'U').slice(0, 1).toUpperCase();

  document.body.insertAdjacentHTML('afterbegin', `
    <div class="app">
      <aside class="sidebar" id="sidebar">
        <a class="brand" href="/studio">
          <img class="brand-mark" src="/assets/logo-mark.png" alt="Viora AI">
          <div class="brand-name">Viora AI<small>AI Video</small></div>
        </a>
        <nav class="nav">
          <div class="nav-group">创作</div>${createNav}
          <div class="nav-group">账户</div>${accountNav}
          ${adminNav}
        </nav>
        <div class="sidebar-foot">
          <div class="row" style="padding:6px 8px;cursor:pointer" id="userMenu">
            <div class="avatar">${initial}</div>
            <div style="min-width:0;flex:1">
              <div class="ellipsis" style="font-weight:600;font-size:13px">${user?.username || '未登录'}</div>
              <div class="ellipsis muted" style="font-size:11.5px">${user?.email || ''}</div>
            </div>
          </div>
        </div>
      </aside>
      <div class="main">
        <header class="topbar">
          <button class="btn btn-ghost btn-sm" id="menuBtn" style="display:none">☰</button>
          <div class="topbar-title">${title}</div>
          <div class="topbar-spacer"></div>
          <div class="pill-balance" id="balancePill" title="点击充值">
            ${ICON.spark}<b id="balanceVal">—</b><span style="font-size:12px;opacity:.8">积分</span>
          </div>
          <button class="btn btn-sm" id="logoutBtn">${ICON.logout}<span>退出</span></button>
        </header>
        <main class="content" id="content"></main>
      </div>
    </div>
  `);

  const sb = $('#sidebar');
  const menuBtn = $('#menuBtn');
  if (window.innerWidth <= 960) menuBtn.style.display = 'inline-flex';
  menuBtn.onclick = () => sb.classList.toggle('open');
  $('#balancePill').onclick = () => (location.href = '/wallet');
  $('#logoutBtn').onclick = async () => {
    await post('/api/auth/logout').catch(() => {});
    location.href = '/login';
  };
  $('#userMenu').onclick = () => (location.href = '/wallet');
  return document.getElementById('content');
}

async function requireUser() {
  try {
    const r = await get('/api/auth/me');
    return r.user;
  } catch (e) {
    if (e.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); return null; }
    throw e;
  }
}

async function refreshBalance() {
  try {
    const r = await get('/api/wallet');
    const v = $('#balanceVal');
    if (v) v.textContent = fmt.points(r.wallet.available);
    if (r.wallet.frozen > 0 && v) v.title = '冻结 ' + r.wallet.frozen + ' 积分';
    return r;
  } catch { return null; }
}

/* ---------------------------------------------------------------- 复制 */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('已复制', 'ok');
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); ta.remove();
    toast('已复制', 'ok');
  }
}

/* ---------------------------------------------------------------- 下载 */
/* 桌面客户端里走本地保存（可以直接落到用户指定目录），网页端走浏览器下载 */
async function downloadUrl(url, filename) {
  const api = window.pywebview && window.pywebview.api;
  if (api && typeof api.save_url === 'function') {
    toast('正在保存到本地…');
    try {
      const r = await api.save_url(url, filename || '');
      if (r && r.ok) { toast('已保存：' + r.path, 'ok'); return; }
      toast('保存失败：' + ((r && r.error) || '未知错误'), 'bad');
      return;
    } catch (e) {
      /* 桥接异常时退回浏览器下载 */
    }
  }
  const a = document.createElement('a');
  a.href = url;
  if (filename) a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* 顶部进度提示条 */
function busy(on) {
  let bar = $('#busybar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'busybar';
    bar.style.cssText = 'position:fixed;top:0;left:0;height:2px;background:linear-gradient(90deg,#FF8A5B,#FF5A1F);z-index:999;transition:width .3s,opacity .3s;width:0';
    document.body.appendChild(bar);
  }
  if (on) { bar.style.opacity = '1'; bar.style.width = '70%'; setTimeout(() => { bar.style.width = '88%'; }, 300); }
  else { bar.style.width = '100%'; setTimeout(() => { bar.style.opacity = '0'; bar.style.width = '0'; }, 300); }
}

/* 轮询助手：页面隐藏时暂停，回来后补齐 */
function poller(fn, ms) {
  let timer = null, stopped = false;
  const run = async () => {
    if (stopped) return;
    if (document.hidden) { timer = setTimeout(run, ms); return; }
    try { await fn(); } catch (e) { /* 静默 */ }
    if (!stopped) timer = setTimeout(run, ms);
  };
  timer = setTimeout(run, ms);
  return { stop() { stopped = true; clearTimeout(timer); } };
}

/* ---------------------------------------------------------------- 人机验证
 * 统一封装：极验 GeeTest v4（首选）与自绘图形码（兜底）。
 *
 *   await Gt4.init()           读 /api/auth/captcha/config，决定用哪种形态
 *   Gt4.mode                   'geetest' | 'image' | 'off'
 *   await Gt4.run()            触发一次验证
 *        极验 → resolve {lot_number, captcha_output, pass_token, gen_time}
 *        图形 → resolve {image: true}（由调用方自己取图、读输入框）
 *        关闭 → resolve {}
 *        用户关弹窗 / 加载失败 → resolve null
 *
 * 注意：极验的一次验证参数只能用一次，注册这类「发码 + 提交」两处都要验证的场景，
 * 必须在每次动作前重新 run()。
 */
const Gt4 = (() => {
  const SCRIPT = 'https://static.geetest.com/v4/gt4.js';
  let mode = 'off';
  let captchaId = '';
  let inst = null;
  let loading = null;       // 正在加载脚本的 Promise，避免并发重复插 script
  let pending = null;       // 当前这一轮 showCaptcha 的 resolve

  function scriptReady() {
    return typeof window.initGeetest4 === 'function';
  }

  async function loadScript() {
    if (scriptReady()) return true;
    if (!loading) {
      loading = new Promise((resolve) => {
        const s = document.createElement('script');
        s.src = SCRIPT;
        s.async = true;
        s.onload = () => resolve(scriptReady());
        s.onerror = () => resolve(false);
        document.head.appendChild(s);
      });
    }
    return loading;
  }

  function firstValue(v) {
    // pass_token / captcha_output 偶尔是数组形式，极验要求原样回传字符串
    return Array.isArray(v) ? v[0] : (v == null ? '' : String(v));
  }

  async function build() {
    if (inst) return inst;
    if (!(await loadScript())) return null;
    inst = await new Promise((resolve) => {
      const timer = setTimeout(() => resolve(null), 8000);
      try {
        window.initGeetest4({ captchaId: captchaId, product: 'bind', language: 'zho' }, (cap) => {
          clearTimeout(timer);
          resolve(cap || null);
        });
      } catch (e) {
        clearTimeout(timer);
        resolve(null);
      }
    });
    if (!inst) return null;

    // 事件只绑一次，靠 pending 决定这次回调喂给谁
    const settle = (fn) => {
      const p = pending;
      pending = null;
      if (p) p(fn);
    };
    // v4 不同小版本回调形态不一致：有的给 result，有的只给 captchaObj，
    // 统一以 getValidate() 为准，取不到再退回回调入参
    const readValidate = (arg) => {
      let v = null;
      try { v = (inst.getValidate && inst.getValidate()) || null; } catch { v = null; }
      if (!v && arg && arg.lot_number) v = arg;
      return v || {};
    };
    inst.onSuccess((arg) => {
      const v = readValidate(arg);
      settle({
        lot_number: firstValue(v.lot_number),
        captcha_output: firstValue(v.captcha_output),
        pass_token: firstValue(v.pass_token),
        gen_time: firstValue(v.gen_time),
      });
    });
    if (typeof inst.onClose === 'function') inst.onClose(() => settle(null));
    if (typeof inst.onError === 'function') inst.onError(() => settle(null));
    return inst;
  }

  return {
    get mode() { return mode; },
    get captchaId() { return captchaId; },

    async init() {
      try {
        const r = await api('/api/auth/captcha/config');
        mode = r.provider || 'off';
        captchaId = r.captcha_id || '';
      } catch {
        mode = 'off';
      }
      // 极验脚本拿不到（广告拦截 / 内网）时降级成提示，不静默放行
      if (mode === 'geetest') {
        const ok = await loadScript();
        if (!ok) mode = 'geetest-error';
      }
      return mode;
    },

    async run() {
      if (mode === 'off') return {};
      if (mode === 'image') return { image: true };
      if (mode === 'geetest-error') return null;

      const cap = await build();
      if (!cap) { mode = 'geetest-error'; return null; }

      return new Promise((resolve) => {
        const timer = setTimeout(() => { pending = null; resolve(null); }, 180000);
        pending = (val) => { clearTimeout(timer); resolve(val); };
        try { cap.showCaptcha(); } catch { clearTimeout(timer); pending = null; resolve(null); }
      });
    },

    /* 把 run() 的结果摊进请求体，图形码模式返回空对象 */
    payload(p) {
      if (!p || p.image) return {};
      return {
        lot_number: p.lot_number || '',
        captcha_output: p.captcha_output || '',
        pass_token: p.pass_token || '',
        gen_time: p.gen_time || '',
      };
    },
  };
})();
