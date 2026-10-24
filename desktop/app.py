"""Muse Studio · Windows 桌面客户端

设计目标（与网页版共用一套前端）：
  · 原生窗口：没有地址栏 / 标签页 / 浏览器菜单，关了窗口进程即退出；
  · 记住登录：设备令牌存 %LOCALAPPDATA%，下次启动直接进工作台；
  · 成品落地：视频/图片点「下载」直接写进用户指定的文件夹，不弹浏览器；
  · 自动升级：启动静默检查，新版本后台下完再提示重启；
  · 单实例：重复双击只把已有窗口唤到前台。

依赖：pywebview（Windows 上用 Edge WebView2 渲染，Win10 1809+ 自带运行时）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import webview

APP_NAME = "Muse Studio"
APP_VERSION = "2.0.0"
LOCK_PORT = 47921

BASE_DIR = (Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
CONFIG_PATH = BASE_DIR / "config.json"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / "MuseStudio"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_PATH = DATA_DIR / "client.log"

DEFAULT_CONFIG = {
    "app_title": APP_NAME,
    "url": "https://studio.example.com/studio",
    "window": {"width": 1280, "height": 860, "min_width": 1000, "min_height": 680},
}

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 MuseStudio/" + APP_VERSION)


# ------------------------------------------------------------------ 基础设施
def log(msg: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def load_json(path: Path, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else dict(default)
    except (OSError, json.JSONDecodeError):
        return dict(default)


def save_json(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log("保存失败 %s: %s" % (path, exc))
        return False


CFG = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, DEFAULT_CONFIG)}
SETTINGS = load_json(SETTINGS_PATH, {})


def save_settings(patch: dict) -> None:
    SETTINGS.update(patch)
    save_json(SETTINGS_PATH, SETTINGS)


def _opener():
    """成品下载直连，不读系统代理（代理残留会导致 WinError 10061）。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def server_origin() -> str:
    p = urllib.parse.urlparse(CFG.get("url") or "")
    return "%s://%s" % (p.scheme or "https", p.netloc or "")


def api_base() -> str:
    return server_origin()


# ------------------------------------------------------------------ 单实例
def acquire_single_instance() -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


# ------------------------------------------------------------------ 窗口外观
def dark_titlebar(hwnd: int, dark: bool = True) -> None:
    """Win10 20H1+ / Win11 的深色标题栏，让原生窗口和品牌色统一。"""
    try:
        import ctypes

        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE 的两个版本号
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attr, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:  # noqa: BLE001
        pass


def center_on_screen(width: int, height: int) -> tuple[int, int]:
    try:
        import ctypes

        user32 = ctypes.windll.user32
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        return max(0, (sw - width) // 2), max(0, (sh - height) // 2)
    except Exception:  # noqa: BLE001
        return 80, 60


# ------------------------------------------------------------------ JS API
class Bridge:
    """前端通过 window.pywebview.api.xxx() 调用这些方法。"""

    def __init__(self):
        self.window: webview.Window | None = None
        self._update_state = {"state": "idle", "version": "", "progress": 0, "error": ""}

    # ---- 基本信息 ----
    def info(self) -> dict:
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "origin": server_origin(),
            "download_dir": self.get_download_dir(),
            "update": dict(self._update_state),
        }

    # ---- 下载目录 ----
    def get_download_dir(self) -> str:
        d = str(SETTINGS.get("download_dir") or "")
        if d and Path(d).is_dir():
            return d
        fallback = Path.home() / "Downloads"
        return str(fallback if fallback.is_dir() else Path.home())

    def pick_download_dir(self) -> str:
        try:
            res = self.window.create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                chosen = res[0] if isinstance(res, (list, tuple)) else res
                save_settings({"download_dir": str(chosen)})
                return str(chosen)
        except Exception as exc:  # noqa: BLE001
            log("选择目录失败: %s" % exc)
        return self.get_download_dir()

    def open_download_dir(self) -> bool:
        try:
            os.startfile(self.get_download_dir())  # noqa: S606
            return True
        except OSError:
            return False

    # ---- 保存成品 ----
    def save_url(self, url: str, filename: str = "") -> dict:
        """把成品下载到本地目录。url 支持相对路径（拼上站点源）。"""
        try:
            if url.startswith("/"):
                url = server_origin() + url
            if not filename:
                filename = "musestudio-" + time.strftime("%Y%m%d-%H%M%S") + ".mp4"
            filename = filename.replace("/", "_").replace("\\", "_")
            folder = Path(self.get_download_dir())
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / filename
            if target.exists():
                stem, suffix = target.stem, target.suffix
                target = folder / ("%s-%d%s" % (stem, int(time.time()) % 10000, suffix))

            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with _opener().open(req, timeout=180) as resp, open(target, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            size = target.stat().st_size
            log("已保存 %s (%d bytes)" % (target, size))
            return {"ok": True, "path": str(target), "bytes": size}
        except (urllib.error.URLError, OSError) as exc:
            log("保存失败 %s: %s" % (url, exc))
            return {"ok": False, "error": str(exc)}

    def reveal_file(self, path: str) -> bool:
        try:
            subprocess.Popen(["explorer", "/select,", str(path)])
            return True
        except OSError:
            return False

    # ---- 升级 ----
    def check_update(self) -> dict:
        try:
            req = urllib.request.Request(api_base() + "/api/desktop/version",
                                         headers={"User-Agent": _UA})
            with _opener().open(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "检查更新失败：%s" % exc}
        latest = str(data.get("version") or "")
        need = _version_tuple(latest) > _version_tuple(APP_VERSION)
        return {"ok": True, "latest": latest, "current": APP_VERSION,
                "available": bool(data.get("available")), "need_update": need,
                "size": data.get("size") or 0, "notes": data.get("notes") or ""}

    def start_update(self) -> dict:
        """下载安装包并静默升级，成功后自动重启。"""
        if self._update_state["state"] == "downloading":
            return {"ok": False, "error": "正在下载中"}
        threading.Thread(target=self._download_and_run, daemon=True).start()
        return {"ok": True, "state": "downloading"}

    def update_state(self) -> dict:
        return dict(self._update_state)

    def _download_and_run(self) -> None:
        self._update_state.update({"state": "downloading", "progress": 0, "error": ""})
        try:
            info = self.check_update()
            if not info.get("ok"):
                raise RuntimeError(info.get("error") or "检查更新失败")
            if not info.get("need_update"):
                self._update_state.update({"state": "latest"})
                return
            url = api_base() + "/api/desktop/download"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            setup = DATA_DIR / "update" / "MuseStudio-Setup.exe"
            setup.parent.mkdir(parents=True, exist_ok=True)

            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            total = int(info.get("size") or 0)
            done = 0
            with _opener().open(req, timeout=120) as resp, open(setup, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self._update_state["progress"] = min(99, int(done * 100 / total))
            self._update_state.update({"state": "ready", "progress": 100,
                                       "version": info.get("latest") or ""})
            log("升级包下载完成 %s" % setup)
        except Exception as exc:  # noqa: BLE001
            log("升级失败: %s\n%s" % (exc, traceback.format_exc()))
            self._update_state.update({"state": "failed", "error": str(exc)})

    def install_update(self) -> dict:
        setup = DATA_DIR / "update" / "MuseStudio-Setup.exe"
        if not setup.is_file():
            return {"ok": False, "error": "安装包还没下好"}
        try:
            subprocess.Popen([str(setup), "/SILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS"])
            threading.Timer(1.0, lambda: os._exit(0)).start()
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    # ---- 窗口 ----
    def minimize(self) -> None:
        try:
            self.window.minimize()
        except Exception:  # noqa: BLE001
            pass

    def toggle_maximize(self) -> None:
        try:
            self.window.maximize()
        except Exception:  # noqa: BLE001
            pass

    def quit(self) -> None:
        try:
            save_settings({"last_close": time.strftime("%Y-%m-%d %H:%M:%S")})
        finally:
            os._exit(0)


def _version_tuple(v: str) -> tuple:
    parts = []
    for seg in (v or "0").split("."):
        digits = "".join(c for c in seg if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


# ------------------------------------------------------------------ 启动
def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = acquire_single_instance()
    if lock is None:
        log("已有实例在运行，本次退出")
        return 0

    win = CFG.get("window") or {}
    width = int(win.get("width") or 1280)
    height = int(win.get("height") or 860)
    x, y = center_on_screen(width, height)

    bridge = Bridge()
    url = CFG.get("url") or "https://studio.example.com/studio"
    log("启动 %s -> %s" % (APP_VERSION, url))

    window = webview.create_window(
        str(CFG.get("app_title") or APP_NAME),
        url=url,
        width=width,
        height=height,
        x=x,
        y=y,
        min_size=(int(win.get("min_width") or 1000), int(win.get("min_height") or 680)),
        background_color="#F4F5F8",
        text_select=True,
        easy_drag=False,
        confirm_close=False,
        js_api=bridge,
    )
    bridge.window = window

    def on_loaded():
        try:
            try:
                import ctypes

                hwnd = ctypes.windll.user32.GetActiveWindow()
                dark_titlebar(hwnd)
            except Exception:  # noqa: BLE001
                pass
            # 注入客户端标记，前端据此把「下载」换成「保存到本地」
            window.evaluate_js(
                "window.__MUSE_DESKTOP__ = {version: '%s', origin: '%s'};"
                % (APP_VERSION, server_origin()))
            log("页面已加载")
        except Exception as exc:  # noqa: BLE001
            log("on_loaded 异常: %s" % exc)

    window.events.loaded += on_loaded

    gui = str(SETTINGS.get("engine") or "").strip() or None
    try:
        if gui:
            webview.start(gui=gui, debug=False, private_mode=False)
        else:
            webview.start(debug=False, private_mode=False)
    except TypeError:
        webview.start()
    except Exception as exc:  # noqa: BLE001
        log("启动失败: %s\n%s" % (exc, traceback.format_exc()))
        try:
            ctypes_popup("客户端启动失败：%s\n详见 %s" % (exc, LOG_PATH))  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            pass
        return 1
    return 0


def ctypes_popup(message: str) -> None:
    import ctypes

    ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)


if __name__ == "__main__":
    sys.exit(main())
