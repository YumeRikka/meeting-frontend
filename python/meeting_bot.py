# meeting_bot.py
# 微信群监听 → 解析「@机器人 建会 名称 时间」→ 创建腾讯会议 → 把链接发回群
# 监听方式：免费版 wxauto4 41.x 没有回调式 AddListenChat，改为轮询 GetAllMessage()
#   - 启动后 ChatWith 打开目标群并保持焦点
#   - 每隔 POLL_INTERVAL 秒拉一次群消息，按消息 id 去重，只处理新消息
#   - 命中「艾特机器人 + 会议指令」就建会，用子窗口 SendMsg 回群（不打断监听焦点）
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from wxauto4 import WeChat

import tencent_meeting as tm

# .env 锁定到脚本所在目录，无论从哪个目录启动都能读到
load_dotenv(Path(__file__).resolve().parent / ".env")

LISTEN_GROUP = os.getenv("LISTEN_GROUP", "你的群名")
TRIGGER = os.getenv("TRIGGER") or "建会"
# 取消会议触发词：艾特机器人后，消息含此词即视为取消指令（需附会议号）
CANCEL_TRIGGER = os.getenv("CANCEL_TRIGGER") or "取消会议"
HOST_USERID = os.getenv("MEETING_USERID", "")
# 可选：机器人在群里的显示昵称。填了之后只响应「艾特到这个名字」的消息；
# 不填则任何人的艾特消息只要带「建会」都响应。
BOT_NICKNAME = os.getenv("BOT_NICKNAME", "")
# 轮询间隔（秒），可在 .env 用 POLL_INTERVAL 覆盖
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.5"))


# ---------- 艾特识别 ----------
def extract_mention(text):
    """
    从消息文本里识别艾特，返回 (正文, 是否被艾特)。

    微信 4.x 的艾特在收到的消息里可能表现为两种形式：
      1. 文本形式：'@昵称 建会 ...'
      2. 占位符形式：'￼'（U+FFFC 对象替换符）
    正文可能含换行（如多行「会议名称/会议时间」格式），整段保留交给解析器。
    """
    mentioned = "￼" in text or "@" in text
    body = text.replace("￼", " ").strip()
    body = re.sub(r"@\S+\s*", "", body).strip()
    return body, mentioned


def msg_text(msg):
    """兼容 content 为 str 或 list 的情况，统一成字符串。"""
    c = getattr(msg, "content", "") or ""
    if isinstance(c, list):
        return "".join(str(x) for x in c)
    return str(c)


def _fmt_ts(ts):
    """把（可能是字符串的）时间戳格式化成「MM-DD HH:MM」，失败原样返回。"""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _fmt_invite_time(start_ts, duration_min):
    """格式化成「YYYY/MM/DD HH:MM-HH:MM」（与腾讯会议邀请文案一致）。"""
    start_dt = datetime.fromtimestamp(int(start_ts))
    end_dt = datetime.fromtimestamp(int(start_ts) + int(duration_min) * 60)
    return f"{start_dt.strftime('%Y/%m/%d %H:%M')}-{end_dt.strftime('%H:%M')}"


def _fmt_code(code):
    """会议号统一成「XXX-XXX-XXX」三段式；非 9 位数字原样返回。"""
    digits = re.sub(r"\D", "", str(code or ""))
    if len(digits) == 9:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:9]}"
    return str(code or "")


def _extract_meeting_code(text):
    """从文本里抽取 9 位会议号（忽略连字符/空格），找不到返回 None。"""
    for token in re.findall(r"[\d\- ]+", text):
        d = re.sub(r"\D", "", token)
        if len(d) == 9:
            return d
    return None


# ---------- 时间解析 ----------
def parse_start_time(text):
    now = datetime.now()
    base = now.date()
    if "后天" in text:
        base = base + timedelta(days=2)
    elif "明天" in text:
        base = base + timedelta(days=1)
    week_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    mw = re.search(r"周([一二三四五六日天])", text)
    if mw:
        target = week_map[mw.group(1)]
        days = (target - base.weekday()) % 7
        base = base + timedelta(days=days)

    md = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    # 分钟部分：不能吃「小时/分钟」前的数字，也不能只吃长数字的一部分
    # （如 "3点 30分钟" 的 30、"9点 1小时" 的 1 都不能当分钟）
    mt = re.search(r"(\d{1,2})\s*[:：点]\s*(\d{1,2})?(?![0-9])(?!\s*(小时|分钟))", text)
    if not mt:
        return int(now.timestamp()) + 120  # 没识别到时间 → 默认现在+2分钟
    h = int(mt.group(1))
    mi = int(mt.group(2)) if mt.group(2) else 0
    if mi == 0 and "半" in text:
        mi = 30
    if ("下午" in text or "晚上" in text or "今晚" in text) and h < 12:
        h += 12
    elif "上午" in text and h == 12:
        h = 0
    elif h <= 6 and not any(w in text for w in ("上午", "下午", "晚上", "早上", "中午", "凌晨", "清晨")):
        h += 12  # 没写时段词的 1-6 点按下午算（会议场景极少凌晨开会）

    if md:
        dt = datetime(int(md.group(1)), int(md.group(2)), int(md.group(3)), h, mi)
    else:
        dt = datetime(base.year, base.month, base.day, h, mi)
    return int(dt.timestamp())


def parse_duration(text):
    mins = 0
    mh = re.search(r"(\d+)\s*小时", text)
    if mh:
        mins += int(mh.group(1)) * 60
    mm = re.search(r"(\d+)\s*分钟?", text)
    if mm:
        mins += int(mm.group(1))
    return mins or 30


def parse_request(text):
    """从群消息里抽出 (主题, 开始时间戳, 时长分钟)；不是建会指令返回 None。"""
    text = text.strip()
    if not text:
        return None

    # 路径A：标签式多行格式「会议名称：xxx / 会议时间：X月X日 HH:MM-HH:MM」
    if "会议名称" in text:
        return parse_labeled(text)

    # 路径B：命令式「建会 主题 时间 时长」
    if TRIGGER not in text:
        return None
    body = text.split(TRIGGER, 1)[1].strip()

    subject = body
    subject = re.sub(r"\d{4}-\d{2}-\d{2}", "", subject)
    subject = re.sub(r"周[一二三四五六日天]", "", subject)
    subject = re.sub(r"(今天|明天|后天|今晚|上午|下午|晚上)", "", subject)
    # 先移除时长（否则 "3点 30分钟" 里的 "30" 会被时间正则吃掉）
    subject = re.sub(r"\d+\s*(小时|分钟?)", "", subject)
    # 时间点：支持 "15:00" "3点" "3点半"（半 直接吃进时间正则，避免残留）
    subject = re.sub(r"\d{1,2}\s*[:：点]\s*(半|\d{0,2})", "", subject)
    subject = re.sub(r"[，。.@|、]", " ", subject)
    subject = re.sub(r"\s+", " ", subject).strip()

    start = parse_start_time(body)
    duration = parse_duration(body)
    return (subject or "会议", start, duration)


def parse_labeled(text):
    """解析「会议名称：xxx\n会议时间：X月X日 HH:MM-HH:MM」格式（支持单行/多行）。"""
    subject, time_str = "", ""
    lines = text.splitlines()
    # 单行混合：会议名称/会议时间 在同一行
    if len(lines) == 1 and "会议名称" in lines[0] and "会议时间" in lines[0]:
        m = re.search(r"会议名称\s*[:：]?\s*(.+?)\s*会议时间\s*[:：]?\s*(.+)", text)
        if m:
            subject, time_str = m.group(1).strip(), m.group(2).strip()
    else:
        for line in lines:
            line = line.strip()
            if "会议名称" in line:
                subject = re.sub(r"^.*?会议名称\s*[:：]?", "", line).strip()
            elif "会议时间" in line:
                time_str = re.sub(r"^.*?会议时间\s*[:：]?", "", line).strip()

    start, duration = parse_time_label(time_str)
    return (subject or "会议", start, duration)


def parse_time_label(s):
    """解析起止时间段写法，如「8月13日 11:00-12:00」。时长 = 结束 - 开始。"""
    now = datetime.now()
    mm = re.search(r"(\d{1,2})\s*月", s)
    dd = re.search(r"(\d{1,2})\s*日", s)
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if mm and dd:
        base = now.replace(
            month=int(mm.group(1)), day=int(dd.group(1)),
            hour=0, minute=0, second=0, microsecond=0,
        )
    elif iso:
        base = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    elif "后天" in s:
        base = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif "明天" in s:
        base = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        base = now  # 没写日期则按今天

    times = re.findall(r"(\d{1,2}):(\d{2})", s)
    if len(times) >= 2:
        h1, m1 = int(times[0][0]), int(times[0][1])
        h2, m2 = int(times[1][0]), int(times[1][1])
        start = base.replace(hour=h1, minute=m1)
        end = base.replace(hour=h2, minute=m2)
        duration = max(1, int((end - start).total_seconds() // 60))
    elif len(times) == 1:
        h1, m1 = int(times[0][0]), int(times[0][1])
        start = base.replace(hour=h1, minute=m1)
        duration = 60
    else:
        start = now + timedelta(minutes=2)
        duration = 30
    return int(start.timestamp()), duration


# ---------- 指令判定 ----------
def try_parse(content):
    """给定一条消息文本，判断是否针对本机器人的指令。返回：
       ("create", 主题, 开始时间戳, 时长分) | ("cancel", 会议号9位数字或None) | None（非指令）
    """
    body, mentioned = extract_mention(content)
    if not mentioned:
        return None
    # 可选：只响应艾特到机器人昵称（或 ￼ 占位符）的消息
    if BOT_NICKNAME and (BOT_NICKNAME not in content and "￼" not in content):
        return None
    # 取消会议优先于建会
    if CANCEL_TRIGGER in body:
        return ("cancel", _extract_meeting_code(body))
    result = parse_request(body)
    if not result:
        return None
    subject, start, duration = result
    return ("create", subject, start, duration)


# ---------- 消息处理主循环 ----------
def main():
    if not tm.get_candidate_accounts():
        print("⚠️ 请在 .env 设置 MEETING_ACCOUNTS / MEETING_USERIDS / MEETING_USERID（企业通讯录里的真实 userid，不是手机号）")

    wx = WeChat()
    # 打开目标群并保持焦点，轮询就靠读这个窗口
    wx.ChatWith(LISTEN_GROUP)
    # 注：本版本 wxauto4 的 GetSubWindow 内部依赖 get_sub_wnd（未实现），故回消息直接用
    # wx.SendMsg(reply, 群名)，它会切到该群发送并保持焦点，便于继续轮询。

    seen = set()          # 已处理过的消息 id / 特征值
    first_poll = True     # 首轮只建索引不建会，避免把历史消息当新指令
    loop = 0

    print(f"开始监听群「{LISTEN_GROUP}」（轮询模式，间隔 {POLL_INTERVAL}s）")
    print(f"触发方式：艾特机器人后发「{TRIGGER} 主题 时间 时长」或带「会议名称/会议时间」标签建会；发「{CANCEL_TRIGGER} 会议号」取消会议")
    if BOT_NICKNAME:
        print(f"艾特校验：只响应艾特「{BOT_NICKNAME}」的消息")
    print("Ctrl+C 退出。")

    try:
        while True:
            loop += 1
            # 每 30 轮重新聚焦一次，防止你手动切走窗口后读错聊天
            if loop % 30 == 0:
                try:
                    wx.ChatWith(LISTEN_GROUP)
                except Exception:
                    pass

            try:
                msgs = wx.GetAllMessage()
            except Exception as e:
                print("[警告] 拉取消息失败:", e)
                time.sleep(POLL_INTERVAL)
                continue

            # 倒序不重要，按返回顺序遍历即可；新消息在末尾
            for msg in msgs:
                if getattr(msg, "attr", "") == "self":
                    continue  # 跳过自己发的
                text = msg_text(msg)
                if not text.strip():
                    continue

                key = getattr(msg, "id", None)
                if key is None:
                    key = f"{getattr(msg, 'sender', '')}|{text}"
                if key in seen:
                    continue
                seen.add(key)
                if len(seen) > 500:  # 控制内存
                    seen.clear()

                if first_poll:
                    continue  # 首轮只建索引

                result = try_parse(text)
                if not result:
                    continue

                action = result[0]
                if action == "cancel":
                    _, code = result
                    if not code:
                        reply = ("⚠️ 未识别到会议号。请在「取消会议」后附上会议号，例如：\n"
                                 "取消会议 662-505-003")
                    else:
                        try:
                            found = tm.find_meeting_by_code(code)
                            if not found:
                                reply = (f"⚠️ 在已配置账号的会议列表里没找到会议号 {_fmt_code(code)} 对应的会议"
                                         f"（可能已取消/已结束/不属于这些账号）")
                            else:
                                name, uid, m = found
                                tm.cancel_meeting(uid, m.get("meeting_id"))
                                reply = (
                                    f"✅ 会议已取消\n"
                                    f"会议主题：{m.get('subject', '会议')}\n"
                                    f"会议号：{_fmt_code(m.get('meeting_code', code))}\n"
                                    f"原账号：{name}"
                                )
                        except Exception as e:
                            print("[错误]", e)
                            reply = f"⚠️ 取消会议失败：{e}"
                else:  # create
                    _, subject, start, duration = result
                    print(f"[解析] 主题={subject} 开始={start} 时长={duration}分")
                    try:
                        res = tm.create_meeting_smart(subject, start, duration)
                        if res["ok"]:
                            invite_time = _fmt_invite_time(start, duration)
                            code = _fmt_code(res["code"])
                            reply = (
                                f"{res['account']} 邀请您参加腾讯会议\n"
                                f"会议主题：{subject}\n"
                                f"会议时间：{invite_time}\n"
                                f"入会链接：{res['url']}\n"
                                f"会议号：{code}"
                            )
                            if res.get("host_key"):
                                reply += f"\n主持人密钥：{res['host_key']}"
                        else:
                            buf_min = res["buffer"] // 60
                            lines = [
                                f"⚠️ 请求时段（前后各 {buf_min} 分钟缓冲）下，所有会议账号均存在时间冲突，未创建会议："
                            ]
                            for name, info in res["details"].items():
                                if info.get("error"):
                                    lines.append(f"• {name}（{info['userid']}）：查询失败 - {info['error']}")
                                    continue
                                if not info["conflicts"]:
                                    # 理论上不会走到这里（有空账号应已被选中）；仅作兜底
                                    lines.append(f"• {name}（{info['userid']}）：无冲突")
                                    continue
                                conf = "；".join(
                                    f"{m.get('subject', '会议')}（{_fmt_ts(m.get('start_time'))}–{_fmt_ts(m.get('end_time'))}）"
                                    for m in info["conflicts"]
                                )
                                lines.append(f"• {name}（{info['userid']}）：冲突 {conf}")
                            reply = "\n".join(lines)
                    except Exception as e:
                        print("[错误]", e)
                        reply = f"⚠️ 建会失败：{e}"
                try:
                    wx.SendMsg(reply, LISTEN_GROUP)
                except Exception as e:
                    print("[警告] 回消息失败:", e)

            first_poll = False
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\n已退出监听。")


if __name__ == "__main__":
    main()
