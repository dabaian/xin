"""生成任务引擎。

状态机（V3 §7）：
  CREATED → RESERVED → QUEUED → SUBMITTING → SUBMITTED → RUNNING
                                            ↘ SUCCEEDED / FAILED / CANCELLED / NEEDS_REVIEW

三条铁律：
 1. 提交前先冻结积分（同一事务），余额不足直接拒绝；
 2. 只有「成品已经安全落盘」才结算扣费，最终失败一律解冻；
 3. 上游超时 != 失败。已经有 provider_task_id 的，只查单不重提，避免重复收费。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid

from . import db, ledger
from .config import CFG, DEFAULT_RESOLUTION, normalize_resolution, resolution_multiplier
from .providers.base import GenRequest, ProviderError
from .providers.muse import provider_from_row

log = logging.getLogger("studio.tasks")

MAX_ATTEMPTS = 2
# 单个任务最长存活时间：超过就转人工对账，不无限转圈
MAX_TASK_AGE = 45 * 60
POLL_BACKOFF_LADDER = [3, 6, 12, 20, 30]

_ALLOWED = {
    "CREATED": {"RESERVED", "FAILED"},
    "RESERVED": {"QUEUED", "FAILED", "CANCELLED"},
    "QUEUED": {"SUBMITTING", "CANCELLED", "FAILED"},
    "SUBMITTING": {"SUBMITTED", "QUEUED", "FAILED", "NEEDS_REVIEW"},
    "SUBMITTED": {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"},
    "RUNNING": {"SUCCEEDED", "FAILED", "CANCELLED", "NEEDS_REVIEW"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "CANCELLED": set(),
    "NEEDS_REVIEW": {"SUCCEEDED", "FAILED", "CANCELLED"},
}


def _ext_for(mime: str, url: str = "") -> tuple[str, str]:
    m = (mime or "").lower()
    if "mp4" in m:
        return ".mp4", "video"
    if "webm" in m:
        return ".webm", "video"
    if "quicktime" in m or "mov" in m:
        return ".mov", "video"
    if "png" in m:
        return ".png", "image"
    if "jpeg" in m or "jpg" in m:
        return ".jpg", "image"
    if "webp" in m:
        return ".webp", "image"
    low = (url or "").lower().split("?")[0]
    for ext in (".mp4", ".webm", ".mov", ".png", ".jpg", ".jpeg", ".webp"):
        if low.endswith(ext):
            return ext, ("video" if ext in (".mp4", ".webm", ".mov") else "image")
    # 号池默认出 mp4
    return ".mp4", "video"


# ------------------------------------------------------------------ 报价
def quote(conn, duration: int, resolution: str = DEFAULT_RESOLUTION, kind: str = "video") -> int:
    """报价：图片 5 积分/张；视频 1080p 为 2 积分/秒（5 秒 10、10 秒 20），2K 翻倍。"""
    if kind == "image":
        row = conn.execute(
            "SELECT points FROM pricing_rules WHERE provider_code='muse' AND model='muse-image' "
            "AND active = 1 ORDER BY id DESC LIMIT 1").fetchone()
        return int(row["points"]) if row else CFG.points_per_image
    res = normalize_resolution(resolution)
    row = conn.execute(
        "SELECT points FROM pricing_rules WHERE provider_code='muse' AND model='muse-video' "
        "AND duration <= ? AND resolution = ? AND active = 1 "
        "ORDER BY duration DESC LIMIT 1", (int(duration), res)).fetchone()
    if row:
        return int(row["points"])
    # 价格表缺档时兜底：基准每秒积分 × 时长 × 清晰度倍率，宁可少收也不错扣
    return max(1, int(duration) * CFG.points_per_second * resolution_multiplier(res))


# ------------------------------------------------------------------ 提交
def create_task(conn, user_id: int, prompt: str, params: dict,
                idempotency_key: str = "", client_ip: str = "",
                thread_key: str = "", image_url: str = "", kind: str = "video") -> dict:
    """在一个事务里完成：建任务 → 冻结积分 → 入队。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("提示词不能为空")
    if len(prompt) > 2000:
        prompt = prompt[:2000]

    kind = "image" if kind == "image" else "video"
    duration = int(params.get("duration") or (0 if kind == "image" else 10))
    resolution = normalize_resolution(params.get("resolution"))
    aspect = str(params.get("aspect_ratio") or ("1:1" if kind == "image" else "9:16"))
    model = "muse-image" if kind == "image" else "muse-video"

    if idempotency_key:
        exist = conn.execute(
            "SELECT * FROM generation_tasks WHERE user_id = ? AND idempotency_key = ?",
            (user_id, idempotency_key)).fetchone()
        if exist:
            return dict(exist)

    active = conn.execute(
        "SELECT COUNT(*) c FROM generation_tasks WHERE user_id = ? AND status IN "
        "('CREATED','RESERVED','QUEUED','SUBMITTING','SUBMITTED','RUNNING')",
        (user_id,)).fetchone()["c"]
    if active >= CFG.max_active_tasks:
        raise ValueError("你有 %d 个任务正在生成，请等其中一个完成再提交" % active)

    points = quote(conn, duration, resolution, kind)
    task_uid = uuid.uuid4().hex
    now = db.now()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO generation_tasks(task_uid, user_id, provider_code, model, status, "
            "prompt, final_prompt, params_json, reserved_points, thread_key, idempotency_key, "
            "client_ip, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_uid, user_id, "muse", model, "CREATED", prompt, "", json.dumps(
                {"kind": kind, "duration": duration, "aspect_ratio": aspect,
                 "resolution": resolution, "image_url": image_url}, ensure_ascii=False),
             points, thread_key, idempotency_key, client_ip, now))
        task_id = cur.lastrowid
        ledger.freeze(conn, user_id, points, "task", task_uid, "提交生成任务冻结")
        conn.execute("UPDATE generation_tasks SET status='QUEUED' WHERE id=?", (task_id,))
        conn.commit()
    except ledger.LedgerError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return dict(conn.execute("SELECT * FROM generation_tasks WHERE id=?", (task_id,)).fetchone())


def _finalize_fail(conn, task, reason: str, code: str = "FAILED",
                   refund: bool = True, status: str = "") -> None:
    """失败收尾：解冻积分 + 落状态。NEEDS_REVIEW 不解冻（等人工裁定）。

    status 只能是 FAILED / CANCELLED / NEEDS_REVIEW；具体原因放 error_code，
    这样前端和统计口径不会被各种细分错误码污染。
    """
    if status not in ("FAILED", "CANCELLED", "NEEDS_REVIEW"):
        status = "NEEDS_REVIEW" if code == "NEEDS_REVIEW" else "FAILED"
    if status == "NEEDS_REVIEW":
        refund = False
    if refund and task["reserved_points"] and task["status"] not in (
            "SUCCEEDED", "FAILED", "CANCELLED"):
        try:
            ledger.release(conn, task["user_id"], task["reserved_points"], "task",
                           task["task_uid"], "生成失败解冻")
        except Exception as exc:  # noqa: BLE001
            log.warning("解冻失败 task=%s: %s", task["task_uid"], exc)
    conn.execute(
        "UPDATE generation_tasks SET status=?, error_code=?, error_message=?, finished_at=? "
        "WHERE id=?", (status, code, reason[:500], db.now(), task["id"]))


def _save_media(conn, task, data: bytes, mime: str, source_url: str,
                result_text: str = "") -> int:
    ext, kind = _ext_for(mime, source_url)
    name = uuid.uuid4().hex + ext
    path = os.path.join(CFG.media_dir, name)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    sha = hashlib.sha256(data).hexdigest()
    cur = conn.execute(
        "INSERT INTO media_assets(user_id, storage_key, kind, mime, bytes, sha256, status, "
        "source, task_id, title, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (task["user_id"], name, kind, mime or ("video/mp4" if kind == "video" else "image/jpeg"),
         len(data), sha, "ready", "generated", task["id"],
         (task["prompt"] or "")[:60], db.now()))
    return cur.lastrowid


# ------------------------------------------------------------------ 单步推进
def step_task(conn, task) -> bool:
    """推进一步。返回 True 表示产生了状态变化。"""
    st = task["status"]
    params = json.loads(task["params_json"] or "{}")

    # ---------- 排队中 → 提交上游 ----------
    if st == "QUEUED":
        prow = conn.execute(
            "SELECT * FROM model_providers WHERE code = ? AND enabled = 1",
            (task["provider_code"],)).fetchone()
        if not prow:
            _finalize_fail(conn, task, "供应商未启用或不存在", "PROVIDER_DISABLED")
            conn.commit()
            return True
        provider = provider_from_row(prow)
        req = GenRequest(
            prompt=task["prompt"], kind=str(params.get("kind") or "video"),
            model=task["model"],
            duration=int(params.get("duration") or 10),
            aspect_ratio=str(params.get("aspect_ratio") or "9:16"),
            resolution=normalize_resolution(params.get("resolution")),
            image_url=str(params.get("image_url") or ""),
            thread_key=task["thread_key"] or ("u%st%s" % (task["user_id"], task["task_uid"][:8])),
            reuse_thread=True,
        )
        # 先标记 SUBMITTING：万一提交过程中进程挂掉，重启后不会盲目重提（防重复收费）
        conn.execute("UPDATE generation_tasks SET status='SUBMITTING', started_at=?, "
                     "attempt_no=attempt_no+1 WHERE id=?", (db.now(), task["id"]))
        conn.commit()
        attempt_no = task["attempt_no"] + 1
        try:
            res = provider.submit(req, task["task_uid"])
        except ProviderError as exc:
            conn.execute(
                "INSERT INTO generation_attempts(task_id, attempt_no, status, error_code, "
                "error_message, started_at, finished_at) VALUES(?,?,?,?,?,?,?)",
                (task["id"], attempt_no, "FAILED", exc.code, exc.message[:400],
                 db.now(), db.now()))
            if exc.retryable and attempt_no < MAX_ATTEMPTS:
                conn.execute("UPDATE generation_tasks SET status='QUEUED', error_message=? "
                             "WHERE id=?", (exc.message[:400], task["id"]))
            else:
                fresh = conn.execute("SELECT * FROM generation_tasks WHERE id=?",
                                     (task["id"],)).fetchone()
                _finalize_fail(conn, fresh, "提交上游失败：%s" % exc.message, "SUBMIT_FAILED")
            conn.commit()
            return True
        conn.execute(
            "INSERT INTO generation_attempts(task_id, attempt_no, provider_task_id, status, "
            "started_at) VALUES(?,?,?,'SUBMITTED',?)",
            (task["id"], attempt_no, res.provider_task_id, db.now()))
        conn.execute("UPDATE generation_tasks SET status='SUBMITTED', provider_task_id=?, "
                     "progress=5 WHERE id=?", (res.provider_task_id, task["id"]))
        conn.commit()
        return True

    # ---------- 已提交 / 运行中 → 查单 ----------
    if st in ("SUBMITTED", "RUNNING"):
        prow = conn.execute("SELECT * FROM model_providers WHERE code = ?",
                            (task["provider_code"],)).fetchone()
        if not prow:
            _finalize_fail(conn, task, "供应商配置丢失", "PROVIDER_DISABLED", refund=False)
            conn.commit()
            return True
        provider = provider_from_row(prow)

        age = _age_seconds(task["created_at"])
        if age > MAX_TASK_AGE:
            _finalize_fail(conn, task, "任务超过 %d 分钟仍未出片，已转人工对账" % (MAX_TASK_AGE // 60),
                           "NEEDS_REVIEW", refund=False)
            conn.commit()
            return True
        if not task["provider_task_id"]:
            _finalize_fail(conn, task, "缺少上游任务号，转人工对账", "NEEDS_REVIEW", refund=False)
            conn.commit()
            return True

        kind = str(params.get("kind") or "video")
        try:
            sr = provider.get_status(task["provider_task_id"], kind)
        except ProviderError as exc:
            if not exc.retryable:
                _finalize_fail(conn, task, "查询上游失败：%s" % exc.message, "POLL_FAILED")
                conn.commit()
                return True
            # 抖动：保持原状态，下一轮再问（前端继续转圈，不吓用户）
            conn.execute("UPDATE generation_tasks SET error_message=? WHERE id=?",
                         ("上游暂时没响应，自动重试中", task["id"]))
            conn.commit()
            return False

        if sr.status == "SUCCEEDED":
            try:
                data, mime = provider.download(sr.media_url)
            except ProviderError as exc:
                # 生成成功但转存失败：不提前结算，重试转存（V3 §10）
                conn.execute("UPDATE generation_tasks SET status='RUNNING', error_message=? "
                             "WHERE id=?", ("成品转存失败，自动重试：%s" % exc.message, task["id"]))
                conn.commit()
                return True
            ext, got_kind = _ext_for(mime, sr.media_url)
            if kind == "video" and got_kind != "video":
                _finalize_fail(conn, task, "期望视频但拿到的是图片（%s），拒绝冒充交付" % ext,
                               "WRONG_MEDIA_TYPE")
                conn.commit()
                return True
            if kind == "image" and got_kind != "image":
                _finalize_fail(conn, task, "期望图片但拿到的是视频（%s），已拒绝交付" % ext,
                               "WRONG_MEDIA_TYPE")
                conn.commit()
                return True
            asset_id = _save_media(conn, task, data, mime, sr.media_url, sr.text)
            ledger.settle(conn, task["user_id"], task["reserved_points"], "task",
                          task["task_uid"], "生成成功结算")
            conn.execute(
                "UPDATE generation_tasks SET status='SUCCEEDED', progress=100, settled_points=?, "
                "result_asset_id=?, result_text=?, provider_task_id=?, finished_at=? WHERE id=?",
                (task["reserved_points"], asset_id, (sr.text or "")[:2000],
                 task["provider_task_id"], db.now(), task["id"]))
            conn.commit()
            return True

        if sr.status in ("FAILED", "CANCELLED"):
            fresh = dict(task)
            _finalize_fail(conn, fresh, sr.error or "上游返回失败",
                           "UPSTREAM_" + sr.status, status=sr.status)
            conn.commit()
            return True

        if sr.status == "NEEDS_REVIEW":
            _finalize_fail(conn, task, sr.error or "上游状态异常，转人工对账",
                           "NEEDS_REVIEW", refund=False)
            conn.commit()
            return True

        # 还在跑
        conn.execute("UPDATE generation_tasks SET status=?, progress=?, queued_ahead=?, "
                     "error_message='' WHERE id=?",
                     (sr.status, max(5, min(99, sr.progress or 0)), sr.queued_ahead,
                      task["id"]))
        conn.commit()
        return True

    # ---------- 提交中断（进程崩溃窗口）→ 保守转人工 ----------
    if st == "SUBMITTING":
        age = _age_seconds(task["started_at"] or task["created_at"])
        if age > 180:
            _finalize_fail(conn, task, "提交过程被中断，为避免重复扣费转人工对账",
                           "NEEDS_REVIEW", refund=False)
            conn.commit()
            return True
    return False


def _age_seconds(ts: str) -> float:
    if not ts:
        return 0
    try:
        from datetime import datetime

        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - t).total_seconds()
    except ValueError:
        return 0


# ------------------------------------------------------------------ 调度器
class TaskRunner:
    """单进程轮询器：足够撑住号池（号池本身就是串行出片）。"""

    def __init__(self, poll: float = 1.5):
        self.poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ticks = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="task-runner", daemon=True)
        self._thread.start()
        log.info("任务调度器已启动")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001
                log.exception("调度器异常：%s", exc)
            self._stop.wait(self.poll)

    def tick(self) -> None:
        self.ticks += 1
        conn = db.connect()
        try:
            # 1) 先提交排队任务（一次一个，避免把号池打满）
            row = conn.execute(
                "SELECT * FROM generation_tasks WHERE status='QUEUED' "
                "ORDER BY id ASC LIMIT 1").fetchone()
            if row:
                step_task(conn, row)
                return
            # 2) 再查在跑的任务
            rows = conn.execute(
                "SELECT * FROM generation_tasks WHERE status IN ('SUBMITTED','RUNNING') "
                "ORDER BY id ASC LIMIT 12").fetchall()
            for r in rows:
                step_task(conn, r)
            # 3) 兜底：被中断的提交
            stuck = conn.execute(
                "SELECT * FROM generation_tasks WHERE status='SUBMITTING' LIMIT 4").fetchall()
            for r in stuck:
                step_task(conn, r)
        finally:
            conn.close()


runner = TaskRunner()


# ------------------------------------------------------------------ 序列化
def public_task(row) -> dict:
    if row is None:
        return {}
    d = dict(row)
    params = {}
    try:
        params = json.loads(d.get("params_json") or "{}")
    except json.JSONDecodeError:
        pass
    return {
        "id": d["task_uid"],
        "status": d["status"],
        "progress": d["progress"],
        "prompt": d["prompt"],
        "kind": params.get("kind") or ("image" if d.get("model") == "muse-image" else "video"),
        "duration": params.get("duration"),
        "aspect_ratio": params.get("aspect_ratio"),
        "resolution": params.get("resolution"),
        "points": d["reserved_points"],
        "asset_id": d["result_asset_id"],
        "text": d.get("result_text") or "",
        "error": d.get("error_message") or "",
        "error_code": d.get("error_code") or "",
        "queued_ahead": d.get("queued_ahead") or 0,
        "created_at": d["created_at"],
        "finished_at": d["finished_at"],
        "media_url": ("/api/media/%s" % d["result_asset_id"]) if d.get("result_asset_id") else "",
    }
