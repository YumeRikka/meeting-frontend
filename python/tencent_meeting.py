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
import logging
from dotenv import load_dotenv

# 模块日志器：app.py 已配置 root logger（控制台+文件双写），
# 这里用子 logger 自动继承，预检/选账号等关键节点都会落盘到 app.log。
_log = logging.getLogger("tm")

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
# 单次 REST 请求超时（秒）：网络不可达时到点即抛错，避免长挂。
REST_TIMEOUT = int(os.getenv("REST_TIMEOUT", "5"))
# 反查总时限（秒）：网页建会后用 REST 拿会议号/链接，到期立即返回 ''，绝不「一直查」。
LOOKUP_TIMEOUT = int(os.getenv("LOOKUP_TIMEOUT", "10"))
# 冲突预检总时限（秒）：查每个账号会议列表的累计上限，到期降级为尽力而为。
# 默认 10s 可覆盖 5 个账号在 REST 可用时的完整预检；若 REST 接口不可用（如生产出口 IP 未加腾讯开放平台白名单），
# 预检必然失败并降级为「按序尝试」，此时与时限无关——需先开通 IP 白名单才能让预检生效。
PRECHECK_TIMEOUT = int(os.getenv("PRECHECK_TIMEOUT", "10"))

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


def _request(method, uri, body_str="", timeout=None):
    """统一发送请求（GET/POST 共用同一套签名），返回解析后的 JSON dict（无内容则返回 {}）；非 200 抛错。
    timeout: 覆盖单次请求超时（秒）；不传则用 REST_TIMEOUT。"""
    import time as _t
    headers = _headers(method, uri, body_str)
    _to = REST_TIMEOUT if timeout is None else timeout
    ts = _t.monotonic()
    try:
        if method == "GET":
            resp = requests.get(f"https://{HOST}{uri}", headers=headers, timeout=_to)
        else:
            resp = requests.post(f"https://{HOST}{uri}", headers=headers, data=body_str.encode("utf-8"), timeout=_to)
    except Exception as e:
        # 超时=5s 却耗时远超 → 卡在 DNS/代理（requests 的 timeout 不覆盖 DNS），属外部网络问题
        print(f"[rest] {method} {uri} 失败，耗时={_t.monotonic()-ts:.2f}s（timeout={_to}s）: {e}", flush=True)
        raise
    print(f"[rest] {method} {uri} 耗时={_t.monotonic()-ts:.2f}s status={resp.status_code}", flush=True)
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


def get_meetings(userid, deadline=None, diag=None):
    """查询某账号的会议列表，返回会议信息 list（每项含 start_time/end_time/subject 等）。自动翻页。
    deadline: 单调时钟截止时间（time.monotonic()），到期即停止翻页，避免长挂。
    diag: 可选 dict，用于回传诊断信息（pages/count/error）。"""
    import time as _t
    meetings = []
    seen = set()
    pos = None
    for _ in range(10):  # 最多翻 10 页，防死循环
        if deadline is not None and _t.monotonic() >= deadline:
            break
        q = f"userid={userid}&instanceid=1"
        if pos is not None:
            q += f"&pos={pos}"
        uri = f"/v1/meetings?{q}"
        # 单次请求超时跟随剩余预算，确保整个调用精确受限
        req_timeout = None
        if deadline is not None:
            remain = deadline - _t.monotonic()
            if remain <= 0:
                break
            req_timeout = max(1.0, min(REST_TIMEOUT, remain))
        try:
            data = _request("GET", uri, timeout=req_timeout)
        except Exception as e:
            if diag is not None:
                diag["error"] = f"请求异常: {e}"
            break
        # 腾讯接口可能在 HTTP 200 体内返回 error_info（如 IP 未加白名单 500125）
        err_info = data.get("error_info") if isinstance(data, dict) else None
        if not err_info and isinstance(data, dict) and data.get("error_code"):
            err_info = {"error_code": data.get("error_code"), "message": data.get("message")}
        if err_info:
            if diag is not None:
                diag["error"] = f"API错误 {err_info.get('error_code')}: {err_info.get('message')}"
            break
        if diag is not None:
            diag["pages"] = diag.get("pages", 0) + 1
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
    if diag is not None:
        diag["count"] = len(meetings)
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


def find_free_account(candidates, start_ts, end_ts, buf, deadline=None):
    """遍历候选账号，返回 (free_name, free_uid, details, status)。
    扫完所有账号再挑第一个无冲突的（而非遇到空闲立即返回），保证 details 完整，
    便于上层日志展示每个账号状态、并让兜底只追加「确空闲/未知」账号。
    details: {name: {"userid":.., "conflicts":[会议...], "error":..或None}}。
    status:
      "free"         -> 找到第一个无冲突账号（free_name 即其名）；details 含全部账号状态
      "query_failed" -> 所有账号的会议列表查询均失败（REST 不可用），退化为尽力而为：
                        返回第一个查询失败的账号，交由上层走网页建会
                        （网页建会本身可用，真实时段冲突由腾讯侧兜底）
      "conflict"     -> 所有账号都存在真实冲突，free_name=None
    deadline: 单调时钟截止时间，到期即停止查询（退化为尽力而为）。
    """
    details = {}
    query_failed = []  # 查询失败的账号（REST 不可用），按顺序记录
    first_free = None  # 第一个确空闲的账号
    for name, uid in candidates:
        try:
            meetings = get_meetings(uid, deadline=deadline)
        except Exception as e:
            details[name] = {"userid": uid, "conflicts": [], "error": str(e)}
            query_failed.append((name, uid))
            continue
        conflicts = [m for m in meetings if _overlaps(start_ts, end_ts, buf, m)]
        details[name] = {"userid": uid, "conflicts": conflicts, "error": None}
        if not conflicts and first_free is None:
            first_free = (name, uid)
    if first_free:
        return first_free[0], first_free[1], details, "free"
    # 没有「确认空闲」的账号：若至少有人是查询失败（非真实冲突），降级为尽力而为
    if query_failed:
        n, u = query_failed[0]
        return n, u, details, "query_failed"
    return None, None, details, "conflict"


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
    """网页建会后，用 REST 查询反查刚建的会议，拿到会议号+入会链接。
    整体受 LOOKUP_TIMEOUT 秒硬上限约束（到期立即返回 ('', '')），绝不「一直查」。
    匹配策略：
      1) 优先按 subject 模糊匹配，取开始时间最靠后（最新创建）的会议；
      2) 若 subject 匹配不到（如网页侧主题被改写/空白），退化为按 start_time ±10 分钟
         精确匹配（我们明确知道请求的开始时间），提高命中率。
    全程打印诊断，便于定位「为什么查不到」（IP 白名单 / 主题不一致 / 首包 DNS 慢等）。
    注意：requests 的 timeout 不覆盖 DNS 解析，单次请求若卡在 DNS 可能超过该超时；
    因此额外用「守护线程 + join(LOOKUP_TIMEOUT)」做终极硬上限——即便某次请求卡死，
    也保证本函数在 LOOKUP_TIMEOUT 秒内必定返回，绝不无限挂起。"""
    import time as _t
    import threading

    WIN = 7 * 24 * 3600  # ±7 天（subject 匹配时用，放宽以防漏）
    TIME_TOL = 600       # ±10 分钟（start_time 兜底匹配）
    result = {"code": "", "url": ""}

    def _run():
        deadline = _t.monotonic() + LOOKUP_TIMEOUT
        attempt = 0
        last_diag = {}
        while _t.monotonic() < deadline:
            attempt += 1
            diag = {}
            try:
                meetings = get_meetings(userid, deadline=deadline, diag=diag)
            except Exception as e:
                meetings = []
                diag["error"] = f"请求异常: {e}"
            # 1) 按 subject 模糊匹配，取开始时间最靠后（最新创建）的
            best, best_st = None, None
            subj_matched = 0
            for m in meetings:
                ms = (m.get("subject") or "")
                if subject not in ms and ms not in subject:
                    continue
                subj_matched += 1
                try:
                    mst = int(m.get("start_time") or 0)
                except (TypeError, ValueError):
                    mst = 0
                if abs(mst - start_ts) > WIN:  # 仅排除明显无关的旧会议（±7 天）
                    continue
                if best_st is None or mst > best_st:
                    best_st, best = mst, m
            # 2) subject 没命中 → 按 start_time ±10 分钟兜底（会议确实已建）
            if best is None:
                for m in meetings:
                    try:
                        mst = int(m.get("start_time") or 0)
                    except (TypeError, ValueError):
                        continue
                    if abs(mst - start_ts) <= TIME_TOL:
                        if best_st is None or mst > best_st:
                            best_st, best = mst, m
            print(f"[lookup] 第{attempt}次：返回 {len(meetings)} 个会议，"
                  f"主题匹配 {subj_matched} 个"
                  + (f"，异常={diag.get('error')}" if diag.get("error") else "")
                  + (f"，命中会议号={best.get('meeting_code')}" if best else "，未命中"), flush=True)
            last_diag = diag
            if best is not None:
                result["code"] = best.get("meeting_code", "")
                result["url"] = best.get("join_url", "")
                return
            sleep_for = min(2.0, max(0.0, deadline - _t.monotonic()))
            if sleep_for > 0:
                _t.sleep(sleep_for)
        print(f"[lookup] {LOOKUP_TIMEOUT}s 内未查到会议（subject='{subject}'，账号={userid}）。"
              f"最后诊断：{last_diag or '无'}。"
              f"可能原因：①生产出口 IP 未加腾讯开放平台白名单（error_code 500125）；"
              f"②网页侧主题与请求不一致；③REST 传播延迟/首包 DNS 解析慢。会议本身应已创建成功。")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(LOOKUP_TIMEOUT)
    if t.is_alive():
        # 单次请求卡死（如首包 DNS 解析挂起，requests 的 timeout 不覆盖 DNS），强制放弃。
        print(f"[lookup] 反查在 LOOKUP_TIMEOUT={LOOKUP_TIMEOUT}s 硬上限被强制中断"
              f"（疑似单次请求卡死，常见于首包 DNS 解析挂起）。会议本身应已创建成功，"
              f"可在腾讯会议客户端查看；如需拿会议号可稍后手动查询或调大 LOOKUP_TIMEOUT。")
        return "", ""
    return result["code"], result["url"]


def create_meeting(subject, start_ts, duration_min, host_userid):
    """创建会议（单账号，兼容旧接口），返回 (join_url, meeting_code)。"""
    if not all([APPID, SDKID, SECRET_ID, SECRET_KEY]):
        raise RuntimeError("缺少腾讯会议凭证，请检查 .env")
    if not host_userid:
        raise RuntimeError("缺少会议账号 userid")
    duration_min = normalize_duration_min(duration_min)
    return _raw_create(subject, start_ts, duration_min, host_userid)


def _is_hard_input_error(e):
    """判断异常是否由表单输入校验失败导致（主题/时间等输入问题）。
    这类错误与账号无关，换账号重试必然复现，应直接抛出而非遍历所有账号空转。"""
    s = str(e)
    keys = ("校验", "全为空格", "会议主题", "停留", "校验错误", "格式不正确",
            "必填", "不能为空", "invalid", "格式", "长度", "still")
    return any(k in s for k in keys)


MIN_MEETING_DURATION_MIN = 15  # 腾讯会议最短会议时长（分钟）


def normalize_duration_min(duration_min):
    """会议时长兜底：腾讯会议要求最少 15 分钟，低于此值表单/接口会拒绝。"""
    return max(int(duration_min or 0), MIN_MEETING_DURATION_MIN)


def _fmt_meeting(m):
    """把会议列表项压缩成一行可读文本，便于日志展示冲突来源。"""
    subj = m.get("subject") or ""
    try:
        s = time.strftime("%m-%d %H:%M", time.localtime(int(m.get("start_time") or 0)))
        e = time.strftime("%m-%d %H:%M", time.localtime(int(m.get("end_time") or 0)))
    except (TypeError, ValueError):
        s = e = "?"
    code = m.get("meeting_code", "")
    return f"{subj}({code}) {s}–{e}"


def log_precheck(details, status, free_name, free_uid):
    """把冲突预检结果打到日志：每个账号是空闲 / 冲突（哪些会）/ 查询失败，以及最终选定谁。"""
    lines = []
    for name, info in details.items():
        if info.get("error"):
            lines.append(f"  - {name}: 查询失败({info['error']})")
        elif info.get("conflicts"):
            cs = "; ".join(_fmt_meeting(m) for m in info["conflicts"])
            lines.append(f"  - {name}: 冲突({cs})")
        else:
            lines.append(f"  - {name}: 空闲")
    if status == "free":
        verdict = f"预检选中空闲账号 {free_name}({free_uid})"
    elif status == "query_failed":
        verdict = f"预检不可用(REST查询失败)，降级用 {free_name}({free_uid}) 尽力而为"
    else:
        verdict = f"预检结果={status}，选定 {free_name}({free_uid})"
    _log.info("冲突预检：\n%s\n  => %s", "\n".join(lines), verdict)


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

    # 腾讯会议最短会议时长 15 分钟，低于此值表单/接口会拒绝，统一兜底到下限
    duration_min = normalize_duration_min(duration_min)

    buf = CONFLICT_BUFFER
    end_ts = start_ts + duration_min * 60
    precheck_deadline = time.monotonic() + PRECHECK_TIMEOUT
    free_name, free_uid, details, status = find_free_account(candidates, start_ts, end_ts, buf, deadline=precheck_deadline)

    # 把「先查会议列表、再挑空闲账号」这一步显式打到日志（控制台 + app.log 双写）
    log_precheck(details, status, free_name, free_uid)
    if on_progress:
        on_progress(0, "已预检会议列表，开始按空闲账号逐个建会")

    if free_name is None:
        # status == "conflict"：所有账号均有真实冲突
        return {"ok": False, "reason": "conflict", "buffer": buf, "details": details}
    if status == "query_failed":
        # 冲突检测依赖的 REST 查询不可用（如接口未开通/凭证问题），不再据此拒绝建会，
        # 降级为「尽力而为」：直接用网页建会（网页建会本身可用，真实冲突由腾讯侧兜底）。
        errs = "; ".join(f"{n}:{d['error']}" for n, d in details.items() if d.get("error"))
        _log.warning("[冲突检测不可用] REST 查询失败（%s），降级为直接网页建会（账号 %s）。真实时段冲突将由腾讯会议侧校验。", errs, free_name)

    prefer = os.getenv("CREATE_METHOD", prefer or "web")
    allow_rest_fallback = os.getenv("ALLOW_REST_FALLBACK", "0") == "1"
    # 组装尝试顺序：
    #  - 预检成功(free)：优先用挑中的空闲账号；兜底只追加「同样空闲/未知」的账号，
    #    不再盲目遍历所有账号（跳过已确认冲突的账号，免得拿它去建会又撞冲突）。
    #  - 预检不可用(query_failed)：按原顺序全部尝试（尽力而为）。
    if status == "free":
        others = [(n, u) for (n, u) in candidates if u != free_uid]
        fallback = [(n, u) for (n, u) in others
                    if not details.get(n, {}).get("conflicts")]  # 跳过已确认冲突的账号
        acct_order = [(free_name, free_uid)] + fallback
        _log.info("[建会决策] 预检选中空闲账号 %s(%s)；兜底候选 %d 个（已排除冲突账号）。",
                  free_name, free_uid, len(fallback))
    else:
        acct_order = [(free_name, free_uid)] + [(n, u) for (n, u) in candidates if u != free_uid]

    if prefer == "web":
        web_errs = []
        for an, au in acct_order:
            # 每一步进度都带上账号名，日志里一眼看出「正在用哪个账号建会」
            def _prog(i, m, _an=an):
                if on_progress:
                    on_progress(i, f"[{_an}] {m}")
            _log.info("[建会] ▶ 开始用账号 %s(%s) 建会（方式=web）", an, au)
            try:
                return _create_via_web(an, au, subject, start_ts, duration_min, on_progress=_prog)
            except Exception as e:
                web_errs.append(f"{an}({au}): {e}")
                # 表单输入校验类错误（主题/时间等）在所有账号上必然复现，换账号重试无意义，直接抛出
                if _is_hard_input_error(e):
                    _log.warning("[建会] ✗ 账号 %s 输入校验失败（换账号必复现，停止重试）：%s", an, e)
                    raise
                reason = str(e) or type(e).__name__
                _log.warning("[建会] ✗ 账号 %s 建会失败，切换到下一个账号。原因：%s", an, reason)
                if on_progress:
                    on_progress(0, f"[{an}] ✗ 建会失败：{reason}（尝试下一个账号）")
                continue
        # 所有账号网页建会均失败
        if allow_rest_fallback:
            _log.warning("[所有账号网页建会失败，回退 REST] %s", "; ".join(web_errs))
        else:
            raise RuntimeError("网页建会失败（已禁用 REST 回退以保留配额）：" + " | ".join(web_errs))

    url, code = _raw_create(subject, start_ts, duration_min, free_uid)
    return {
        "ok": True,
        "url": url,
        "code": code,
        "account": free_name,
        "userid": free_uid,
        "buffer": buf,
        "host_key": HOST_KEY,
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
        "host_key": res.get("host_key", ""),
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

