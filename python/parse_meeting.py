"""
会议文本解析模块（与微信 bot 解析逻辑保持一致，单一数据源）

从用户粘贴的纯文本里解析出 (会议主题, 开始时间戳, 时长分钟)。
支持两种写法：
  A. 标签式（推荐，网页端主用）：
       会议名称：xxx
       会议时间：8月16日 18:00-19:00
  B. 自由式（无标签，兼容微信 bot 的宽松写法）：
       周会 明天 15:00 1小时

本模块不依赖 wxauto4 / tencent_meeting，可安全在 Flask 后端直接 import。
解析函数原样移植自 meeting_bot.py，确保前后端行为一致。
"""

import re
from datetime import datetime, timedelta


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

    times = re.findall(r"(\d{1,2})[:：](\d{2})", s)
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


def _extract_labeled(text):
    """从标签式文本里抽出 (会议主题, 时间字符串)。"""
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
            if "会议名称" in line or "会议主题" in line:
                subject = re.sub(r"^.*?(?:会议名称|会议主题)\s*[:：]?", "", line).strip()
            elif "会议时间" in line:
                time_str = re.sub(r"^.*?会议时间\s*[:：]?", "", line).strip()
    return subject, time_str


def parse_meeting_text(text):
    """
    解析用户粘贴的会议文本。

    返回 dict：
        {"subject": str, "start": int(时间戳), "duration": int(分钟), "time_found": bool}
    或 None（文本为空）。

    time_found 为 False 表示未能从文本中识别到任何时间点，调用方应拒绝并提示格式。
    """
    text = (text or "").strip()
    if not text:
        return None

    # 路径 A：标签式（会议名称/会议主题/会议时间 任一出现即走标签解析）
    if any(k in text for k in ("会议名称", "会议主题", "会议时间")):
        subject, time_str = _extract_labeled(text)
        start, duration = parse_time_label(time_str)
        time_found = bool(re.search(r"\d{1,2}[:：]\d{2}", time_str))
        return {
            "subject": subject or "会议",
            "start": start,
            "duration": duration,
            "time_found": time_found,
        }

    # 路径 B：自由式（无标签）—— 与微信 bot 的宽松写法兼容
    subject = text
    subject = re.sub(r"\d{4}-\d{2}-\d{2}", "", subject)
    subject = re.sub(r"周[一二三四五六日天]", "", subject)
    subject = re.sub(r"(今天|明天|后天|今晚|上午|下午|晚上)", "", subject)
    subject = re.sub(r"\d+\s*(小时|分钟?)", "", subject)
    subject = re.sub(r"\d{1,2}\s*[:：点]\s*(半|\d{0,2})", "", subject)
    subject = re.sub(r"[，。.@|、]", " ", subject)
    subject = re.sub(r"\s+", " ", subject).strip()

    start = parse_start_time(text)
    duration = parse_duration(text)
    time_found = bool(re.search(r"\d{1,2}[:：]\d{2}", text))
    return {
        "subject": subject or "会议",
        "start": start,
        "duration": duration,
        "time_found": time_found,
    }


if __name__ == "__main__":
    # 简单自测
    samples = [
        "会议名称：周会\n会议时间：8月16日 18:00-19:00",
        "会议名称：项目评审 会议时间：2026-08-20 10:30-11:30",
        "英语课 明天 15:00 1小时",
        "会议名称：晨会",  # 无时间
    ]
    for s in samples:
        print("输入:", repr(s))
        print("  →", parse_meeting_text(s))
