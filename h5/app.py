"""
会议卡密 H5 后端（Flask）

架构：
  客户在 H5 输入卡密 → /api/card/verify 校验额度与有效期
                → /api/meeting/create 建会（走 tencent_meeting.create_meeting_smart 额度池自动挑空闲账号）
                → /api/meeting/cancel 取消（按会议号定位并取消）
  管理员 → /api/admin/cards 批量生成卡密（ADMIN_TOKEN 保护）

部署：本文件 + static/ 由同一 Flask 服务托管（同源，无需 CORS）。
      若后续把静态前端放 EdgeOne Makers、后端另托管，已开启宽松 CORS 头。
"""

import os
import sys
import time
import threading
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory

BASE = Path(__file__).resolve().parent
PY = BASE.parent / "python"
sys.path.insert(0, str(PY))

import cards
import tencent_meeting as tm
from dotenv import load_dotenv

# 载入配置：优先 h5/.env，再回退 python/.env（复用腾讯会议凭证与多账号配置）
load_dotenv(str(PY / ".env"))
load_dotenv(str(BASE / ".env"))

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

app = Flask(__name__, static_folder=str(BASE / "static"))
cards.init_db()


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp


def _err(reason, status=400, **extra):
    return jsonify({"ok": False, "reason": reason, **extra}), status


# ---------- 卡密 ----------
@app.route("/api/card/verify", methods=["POST", "OPTIONS"])
def card_verify():
    if request.method == "OPTIONS":
        return "", 204
    code = ((request.json or {}).get("code") or "").strip().upper()
    if not cards.exists(code):
        return _err("not_found")
    res = cards.verify(code)
    if not res["ok"]:
        return _err(res["reason"])
    return jsonify({
        "ok": True,
        "remaining": res["remaining"],
        "quota": res["quota"],
        "expires_at": res["expires_at"],
    })


# ---------- 创建会议（异步任务 + 进度条）----------
TASKS = {}
TASK_LOCK = threading.Lock()


def _emit_progress(task_id, index, message):
    with TASK_LOCK:
        s = TASKS.get(task_id)
        if s:
            s["step"] = index
            s["message"] = message


def _run_create(task_id, code, subject, start, duration):
    try:
        res = tm.create_meeting_smart(
            subject, start, duration,
            on_progress=lambda i, m: _emit_progress(task_id, i, m),
        )
    except Exception as e:
        with TASK_LOCK:
            TASKS[task_id].update({"done": True, "ok": False,
                                   "error": {"reason": "create_failed", "detail": str(e)}})
        return
    if not res["ok"]:
        with TASK_LOCK:
            TASKS[task_id].update({"done": True, "ok": False,
                                   "error": {"reason": res.get("reason"),
                                             "details": res.get("details"),
                                             "buffer": res.get("buffer")}})
        return
    cards.consume(code)
    invite = {
        "account": res["account"],
        "url": res["url"],
        "code": res["code"],
        "subject": subject,
        "start": start,
        "end": start + duration * 60,
        "duration": duration,
        "buffer": res.get("buffer"),
    }
    with TASK_LOCK:
        TASKS[task_id].update({"done": True, "ok": True, "invite": invite})


@app.route("/api/meeting/create", methods=["POST", "OPTIONS"])
def meeting_create():
    if request.method == "OPTIONS":
        return "", 204
    data = request.json or {}
    code = (data.get("code") or "").strip().upper()
    subject = (data.get("subject") or "").strip()
    try:
        start = int(data.get("start") or 0)
        end = int(data.get("end") or 0)
    except (TypeError, ValueError):
        return _err("invalid_params")

    if not subject or not start or not end:
        return _err("invalid_params")
    if end <= start:
        return _err("invalid_params", detail="结束时间需晚于开始时间")

    v = cards.verify(code)
    if not v["ok"]:
        return _err(v["reason"])

    # 按次卡限定时长上限（默认 3 小时）
    max_min = int(v.get("max_duration_min") or 180)
    duration = int(round((end - start) / 60))
    if duration <= 0:
        return _err("invalid_params", detail="会议时长需大于 0")
    if duration > max_min:
        return _err("too_long", status=400, max_min=max_min,
                    detail=f"会议时长不可超过 {max_min} 分钟（{max_min // 60} 小时）")

    # 校验通过后立即返回 task_id；真正的建会（驱动浏览器，耗时较长）在后台线程进行，
    # 前端用 /api/meeting/progress 轮询进度，避免请求长时间挂起。
    task_id = uuid.uuid4().hex
    with TASK_LOCK:
        TASKS[task_id] = {"step": 0, "message": "正在准备…", "done": False,
                          "ok": None, "invite": None, "error": None}
    threading.Thread(target=_run_create, args=(task_id, code, subject, start, duration),
                     daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id})


@app.route("/api/meeting/progress", methods=["GET"])
def meeting_progress():
    task = (request.args.get("task") or "").strip()
    with TASK_LOCK:
        s = TASKS.get(task)
        if not s:
            return _err("not_found", status=404)
        snap = dict(s)
    total = 7
    idx = int(snap.get("step", 0))
    snap["percent"] = int((idx + 1) / total * 100)
    snap["total"] = total
    return jsonify(snap)


# ---------- 取消会议 ----------
@app.route("/api/meeting/cancel", methods=["POST", "OPTIONS"])
def meeting_cancel():
    if request.method == "OPTIONS":
        return "", 204
    data = request.json or {}
    code = (data.get("code") or "").strip().upper()
    meeting_code = (data.get("meeting_code") or "").strip()

    # 取消只需卡密有效（存在/未停用/未过期），不要求仍有额度
    if not cards.exists(code):
        return _err("not_found")
    row_ok = cards.verify(code)
    if not row_ok["ok"] and row_ok["reason"] not in ("no_quota",):
        return _err(row_ok["reason"])

    if not meeting_code:
        return _err("invalid_params")

    try:
        found = tm.find_meeting_by_code(meeting_code)
    except Exception as e:
        return _err("query_failed", status=500, detail=str(e))

    if not found:
        return _err("meeting_not_found",
                    detail="在已配置账号中未找到该会议号，可能已取消/已结束/不属于这些账号")

    name, uid, m = found
    try:
        tm.cancel_meeting(uid, m["meeting_id"])
    except Exception as e:
        return _err("cancel_failed", status=500, detail=str(e))

    return jsonify({
        "ok": True,
        "subject": m.get("subject", ""),
        "meeting_code": meeting_code,
        "account": name,
    })


# ---------- 管理员：生成卡密 ----------
def _admin_ok():
    token = (request.json or {}).get("token") or request.args.get("token") or ""
    return bool(ADMIN_TOKEN) and token == ADMIN_TOKEN


@app.route("/api/admin/cards", methods=["POST", "OPTIONS"])
def admin_create():
    if request.method == "OPTIONS":
        return "", 204
    if not _admin_ok():
        return _err("forbidden", status=403)
    data = request.json or {}
    try:
        count = int(data.get("count") or 1)
        quota = int(data.get("quota") or 1)
    except (TypeError, ValueError):
        return _err("invalid_params")
    days = data.get("days")
    if days not in (None, "", 0):
        try:
            days = int(days)
        except (TypeError, ValueError):
            return _err("invalid_params")
    else:
        days = None
    note = (data.get("note") or "").strip()
    max_dur = data.get("max_duration_min")
    if max_dur in (None, "", 0):
        max_dur = 180
    else:
        try:
            max_dur = int(max_dur)
        except (TypeError, ValueError):
            return _err("invalid_params")
    codes = cards.create_cards(count, quota, days, note, max_dur)
    return jsonify({"ok": True, "codes": codes})


@app.route("/api/admin/cards", methods=["GET"])
def admin_list():
    if not _admin_ok():
        return _err("forbidden", status=403)
    rows = cards.list_cards()
    return jsonify({"ok": True, "cards": rows})


# ---------- 静态前端 ----------
@app.route("/")
def index():
    return send_from_directory(str(BASE / "static"), "index.html")


@app.route("/admin")
def admin_page():
    return send_from_directory(str(BASE / "static"), "admin.html")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
