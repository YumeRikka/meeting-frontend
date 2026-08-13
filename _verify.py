import sys, datetime, time
sys.path.insert(0, "python")
import create_via_web as cw
import tencent_meeting as tm

USERID = sys.argv[1] if len(sys.argv) > 1 else "wemeeting8295134"
# 用之前失败的 case：任意分钟，验证真实键盘能否把时间写进 React state
now = datetime.datetime.now() + datetime.timedelta(days=16)
start = now.replace(hour=10, minute=42, second=0, microsecond=0)
start_ts = int(start.timestamp())
end_ts = start_ts + 30 * 60
SUBJECT = "验证修复-" + datetime.datetime.now().strftime("%m%d%H%M")
print(f"[verify] userid={USERID} subject={SUBJECT}")
print(f"[verify] 请求 start={start} ({start_ts}) end={datetime.datetime.fromtimestamp(end_ts)}")

def on_progress(i, msg=None):
    print(f"  [进度 {i}] {msg}")

res = cw.create_meeting(
    userid=USERID,
    subject=SUBJECT,
    start_ts=start_ts,
    end_ts=end_ts,
    host_key="",
    on_progress=on_progress,
)
print("\n=== create_meeting 返回 ===")
print("ok=", res.get("ok"), "code=", repr(res.get("code")), "url=", repr(res.get("url")))

# 用 REST 反查实际建出的会议时间
time.sleep(3)
print("\n=== REST 实际会议时间 ===")
ms = tm.get_meetings(USERID)
for m in ms:
    if SUBJECT in (m.get("subject") or ""):
        st = int(m.get("start_time") or 0)
        print("  匹配会议:", {
            "subject": m.get("subject"),
            "code": m.get("meeting_code"),
            "start_time": st,
            "start_human": datetime.datetime.fromtimestamp(st).strftime("%Y-%m-%d %H:%M"),
            "url": (m.get("join_url") or "")[:55],
        })
        print(f"  请求={start_ts} 实际={st} 差={st-start_ts}s  ({'✓一致' if abs(st-start_ts)<300 else '✗不一致'})")
        break
else:
    print("  未找到匹配会议")
print("\n[verify] done")
