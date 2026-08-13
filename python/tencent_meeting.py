# tencent_meeting.py
# 腾讯会议 REST API 调用（TC3-HMAC-SHA256 签名），无 GUI 依赖
import os
import re
import time
import hmac
import hashlib
import base64
import json
import requests
from pathlib import Path
from dotenv import load_dotenv

# .env 锁定到脚本所在目录，无论从哪个目录启动都能读到
load_dotenv(Path(__file__).resolve().parent / ".env")

APPID = os.getenv("APPID")
SDKID = os.getenv("SDKID")
SECRET_ID = os.getenv("SECRET_ID")
SECRET_KEY = os.getenv("SECRET_KEY")
# 主持人密钥（可选，通常 6 位数字）。留空则不设；部分套餐需企业版才支持 host_key。
HOST_KEY = os.getenv("HOST_KEY", "")
# 冲突缓冲（秒）：新会议「前后各多少秒」内不能与已有会议重叠，默认 30 分钟。
CONFLICT_BUFFER = int(os.getenv("CONFLICT_BUFFER", "1800"))

HOST = "api.meeting.qq.com"
URI = "/v1/meetings"


def _sign(secret_id, secret_key, method, nonce, timestamp, uri, body):
    # 官方口径：拼接签名串 → HMAC-SHA256 → hex → Base64
    # 注意 GET 请求的 uri 必须包含完整查询串（如 /v1/meetings?userid=x&instanceid=1），body 用空串
    header_str = f"X-TC-Key={secret_id}&X-TC-Nonce={nonce}&X-TC-Timestamp={timestamp}"
    string_to_sign = f"{method}\n{header_str}\n{uri}\n{body}"
    hex_hash = hmac.new(
        secret_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return base64.b64encode(hex_hash.encode("utf-8")).decode("utf-8")


def _headers(method, uri, body_str=""):
    timestamp = str(int(time.time()))
    nonce = str(int(time.time() * 1000) % 1000000000)
    signature = _sign(SECRET_ID, SECRET_KEY, method, nonce, timestamp, uri, body_str)
    return {
        "Content-Type": "application/json",
        "X-TC-Key": SECRET_ID,
        "X-TC-Timestamp": timestamp,
        "X-TC-Nonce": nonce,
        "X-TC-Signature": signature,
        "X-TC-Registered": "1",  # 会议进通讯录；若报通讯录错误改成 "0" 或删掉这行
        "AppId": APPID,
        "SdkId": SDKID,
    }


def _request(method, uri, body_str=""):
    """统一发送请求（GET/POST 共用同一套签名），返回解析后的 JSON dict（无内容则返回 {}）；非 200 抛错。"""
    headers = _headers(method, uri, body_str)
    if method == "GET":
        resp = requests.get(f"https://{HOST}{uri}", headers=headers, timeout=10)
    else:
        resp = requests.post(f"https://{HOST}{uri}", headers=headers, data=body_str.encode("utf-8"), timeout=10)
    if resp.status_code != 200:
        try:
            data = resp.json()
        except Exception:
            data = resp.text
        raise RuntimeError(f"HTTP {resp.status_code}: {data}")
    # 部分接口（如取消会议）成功时返回空 body
    if not resp.text.strip():
        return {}
    try:
        return resp.json()
    except Exception:
        return {}


def get_meetings(userid):
    """查询某账号的会议列表，返回会议信息 list（每项含 start_time/end_time/subject 等）。自动翻页。"""
    meetings = []
    seen = set()
    pos = None
    for _ in range(10):  # 最多翻 10 页，防死循环
        q = f"userid={userid}&instanceid=1"
        if pos is not None:
            q += f"&pos={pos}"
        uri = f"/v1/meetings?{q}"
        data = _request("GET", uri)
        for m in data.get("meeting_info_list", []):
            mid = m.get("meeting_id")
            if mid in seen:
                continue
            seen.add(mid)
            meetings.append(m)
        remaining = data.get("remaining", 0)
        next_pos = data.get("next_pos")
        if not remaining or next_pos is None:
            break
        pos = next_pos
    return meetings


def get_candidate_accounts():
    """从 .env 解析候选会议账号，返回 [(显示名, userid), ...]。
    优先级：MEETING_ACCOUNTS（name:userid 逗号分隔）> MEETING_USERIDS（逗号分隔）> MEETING_USERID（单账号）。
    """
    raw = os.getenv("MEETING_ACCOUNTS", "").strip()
    accounts = []
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                name, uid = item.split(":", 1)
                accounts.append((name.strip(), uid.strip()))
            else:
                accounts.append((item, item))
    if not accounts:
        ids = os.getenv("MEETING_USERIDS", "")
        for uid in ids.split(","):
            uid = uid.strip()
            if uid:
                accounts.append((uid, uid))
    if not accounts:
        single = os.getenv("MEETING_USERID", "")
        if single:
            accounts.append((single, single))
    return accounts


def _overlaps(start_ts, end_ts, buf, m):
    """判断已有会议 m 是否与新会议 [start_ts,end_ts] 在 ±buf 缓冲内冲突。"""
    try:
        m_start = int(m.get("start_time"))
        m_end = int(m.get("end_time"))
    except (TypeError, ValueError):
        return False
    # 新会议前后各 buf 秒都不能与已有会议重叠
    return (start_ts - buf) < m_end and (end_ts + buf) > m_start


def find_free_account(candidates, start_ts, end_ts, buf):
    """遍历候选账号，返回 (free_name, free_uid, details)。
    details: {name: {"userid":.., "conflicts":[会议...], "error":..或None}}。
    找到第一个无冲突账号即返回；若全部冲突，free_name=None。
    """
    details = {}
    for name, uid in candidates:
        try:
            meetings = get_meetings(uid)
        except Exception as e:
            details[name] = {"userid": uid, "conflicts": [], "error": str(e)}
            continue
        conflicts = [m for m in meetings if _overlaps(start_ts, end_ts, buf, m)]
        details[name] = {"userid": uid, "conflicts": conflicts, "error": None}
        if not conflicts:
            return name, uid, details
    return None, None, details


def _raw_create(subject, start_ts, duration_min, host_userid):
    """在指定账号下创建会议，返回 (join_url, meeting_code)。"""
    end_ts = start_ts + duration_min * 60
    # 会议设置：allow_in_before_host=false = 主持人进入前成员不可进入
    settings = {
        "allow_in_before_host": False,
    }
    body = {
        "userid": host_userid,
        "instanceid": 1,
        "subject": subject,
        "type": 0,  # 0=预约会议 1=快速会议
        # 腾讯接口要求时间字段为字符串时间戳（不是数字），否则报 190004
        "start_time": str(start_ts),
        "end_time": str(end_ts),
        "settings": settings,
    }
    # 主持人密钥：腾讯会议要求 enable_host_key + host_key 作为顶层字段（不是放在 settings 里）
    if HOST_KEY:
        body["enable_host_key"] = True
        body["host_key"] = HOST_KEY

    body_str = json.dumps(body, ensure_ascii=False)
    data = _request("POST", URI, body_str)
    if "meeting_info_list" not in data or not data["meeting_info_list"]:
        raise RuntimeError(f"创建失败: {data}")
    m = data["meeting_info_list"][0]
    return m.get("join_url", ""), m.get("meeting_code", "")


def _lookup_meeting_by_subject(userid, subject, start_ts):
    """网页建会后，用 REST 查询（不限频）反查刚建的会议，拿到会议号+入会链接。
    按 subject 模糊匹配 + 开始时间就近（±2 小时窗口）挑选，返回 (meeting_code, join_url)。
    找不到返回 ('', '')。"""
    import time as _t
    start_ts = int(start_ts)
    for _ in range(6):  # 最多重试 6 次（间隔 2s，约 12s），等 REST 侧可见
        try:
            meetings = get_meetings(userid)
        except Exception:
            meetings = []
        best, best_diff = None, None
        for m in meetings:
            ms = (m.get("subject") or "")
            if subject not in ms and ms not in subject:
                continue
            try:
                mst = int(m.get("start_time") or 0)
            except (TypeError, ValueError):
                mst = 0
            if abs(mst - start_ts) > 7200:  # 仅考虑 ±2 小时内的会议，避免误匹配旧会议
                continue
            diff = abs(mst - start_ts)
            if best_diff is None or diff < best_diff:
                best_diff, best = diff, m
        if best is not None:
            return best.get("meeting_code", ""), best.get("join_url", "")
        _t.sleep(2)
    return "", ""


def create_meeting(subject, start_ts, duration_min, host_userid):
    """创建会议（单账号，兼容旧接口），返回 (join_url, meeting_code)。"""
    if not all([APPID, SDKID, SECRET_ID, SECRET_KEY]):
        raise RuntimeError("缺少腾讯会议凭证，请检查 .env")
    if not host_userid:
        raise RuntimeError("缺少会议账号 userid")
    return _raw_create(subject, start_ts, duration_min, host_userid)


def create_meeting_smart(subject, start_ts, duration_min, prefer="web", on_progress=None):
    """智能建会：遍历候选账号找一个请求时段空闲的建会；全冲突则返回冲突明细、不建会。
    建会方式：
      prefer="web"  -> 优先用网页登录建会（不限频，可设名称+主持人密钥）；
                       该账号无网页密码凭证或网页建会失败时，自动回退 REST API（受 12 次/月限制）。
      prefer="rest" -> 只用 REST API 建会。
    on_progress(index, message): 进度回调（透传给网页建会）。
    返回 dict：
      ok=True  -> {ok, url, code, account, userid, buffer, method}
      ok=False -> {ok:False, reason:"conflict", buffer, details:{name:{...}}}
    """
    if not all([APPID, SDKID, SECRET_ID, SECRET_KEY]):
        raise RuntimeError("缺少腾讯会议凭证，请检查 .env")
    candidates = get_candidate_accounts()
    if not candidates:
        raise RuntimeError("未配置任何会议账号（MEETING_ACCOUNTS / MEETING_USERIDS / MEETING_USERID）")

    buf = CONFLICT_BUFFER
    end_ts = start_ts + duration_min * 60
    free_name, free_uid, details = find_free_account(candidates, start_ts, end_ts, buf)
    if free_name is None:
        return {"ok": False, "reason": "conflict", "buffer": buf, "details": details}

    prefer = os.getenv("CREATE_METHOD", prefer or "web")
    allow_rest_fallback = os.getenv("ALLOW_REST_FALLBACK", "0") == "1"
    if prefer == "web":
        try:
            return _create_via_web(free_name, free_uid, subject, start_ts, duration_min, on_progress=on_progress)
        except Exception as e:
            if allow_rest_fallback:
                # 仅在显式开启时回退 REST（会消耗 12 次/月配额，默认不开启）
                print(f"[web-create 失败，回退 REST] {e}")
            else:
                # 默认不回退 REST，避免白白消耗每月创建配额；直接抛出网页建会错误
                raise RuntimeError(f"网页建会失败（已禁用 REST 回退以保留配额）：{e}")

    url, code = _raw_create(subject, start_ts, duration_min, free_uid)
    return {
        "ok": True,
        "url": url,
        "code": code,
        "account": free_name,
        "userid": free_uid,
        "buffer": buf,
        "method": "rest",
    }


def _create_via_web(free_name, free_uid, subject, start_ts, duration_min, on_progress=None):
    """用网页登录方式在 free_uid 账号下建会（不限频）。无凭证/未登录会抛错由上层回退。
    会议号优先从结果页抓取；无头环境结果页崩溃抓不到时，用 REST 反查（也不限频）。"""
    import importlib
    cw = importlib.import_module("create_via_web")
    creds = cw.load_accounts().get(free_uid)
    if not creds or not creds.get("password"):
        raise RuntimeError(f"账号 {free_uid} 未配置网页登录密码（accounts.json）")
    end_ts = start_ts + duration_min * 60
    res = cw.create_meeting(
        userid=free_uid,
        subject=subject,
        start_ts=start_ts,
        end_ts=end_ts,
        host_key=HOST_KEY or creds.get("host_key", ""),
        on_progress=on_progress,
    )
    code, url = res.get("code", ""), res.get("url", "")
    if not (code and url):
        # 网页已提交但结果页未抓到 → REST 反查（会议已创建，不会重复建会）
        code, url = _lookup_meeting_by_subject(free_uid, subject, start_ts)
    if not (code and url):
        print(f"[web-create] 账号 {free_uid} 会议已创建但未能获取会议号/链接，请稍后在会议列表查看")
    return {
        "ok": True,
        "url": url,
        "code": code,
        "account": free_name,
        "userid": free_uid,
        "buffer": CONFLICT_BUFFER,
        "method": "web",
    }


def find_meeting_by_code(code):
    """按会议号（9 位数字，忽略分隔符）在候选账号的会议列表里查找，返回 (account_name, userid, meeting) 或 None。"""
    digits = re.sub(r"\D", "", str(code))
    if len(digits) != 9:
        return None
    for name, uid in get_candidate_accounts():
        try:
            meetings = get_meetings(uid)
        except Exception:
            continue
        for m in meetings:
            if re.sub(r"\D", "", str(m.get("meeting_code", ""))) == digits:
                return name, uid, m
    return None


def cancel_meeting(userid, meeting_id, reason_code=1, reason_detail="机器人取消"):
    """取消会议：POST /v1/meetings/{meeting_id}/cancel。成功返回 True；失败抛 RuntimeError。"""
    if not all([APPID, SDKID, SECRET_ID, SECRET_KEY]):
        raise RuntimeError("缺少腾讯会议凭证，请检查 .env")
    if not userid or not meeting_id:
        raise RuntimeError("缺少取消会议所需的 userid / meeting_id")
    body = {
        "userid": userid,
        "instanceid": 1,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
    }
    body_str = json.dumps(body, ensure_ascii=False)
    uri = f"/v1/meetings/{meeting_id}/cancel"
    _request("POST", uri, body_str)  # 成功时返回空 body
    return True

