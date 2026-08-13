"""验证 H5 后端 API 接线（mock 掉浏览器建会，不依赖 Playwright）。"""
import sys, time
from pathlib import Path
sys.path.insert(0, r"D:\WorkBuddy\meeting-bot\h5")
import cards
# 用独立测试库，避免污染真实 cards.db
cards.DB_PATH = Path(r"D:\WorkBuddy\meeting-bot\h5\cards_test.db")
cards.init_db()
import tencent_meeting as tm
import app as flask_app

# 1) 建一张测试卡（配额3，时长上限180分钟）
codes = cards.create_cards(1, quota=3, days_valid=30, note="api-test", max_duration_min=180)
code = codes[0]
print("测试卡密:", code)

# 2) mock 网页建会：成功
def fake_ok(subject, start_ts, duration, prefer="web"):
    return {"ok": True, "url": "https://meeting.tencent.com/dm/FAKE",
            "code": "123456789", "account": "301", "userid": "admin1783592634",
            "buffer": 1800, "method": "web"}
tm.create_meeting_smart = fake_ok

client = flask_app.app.test_client()
start = int(time.time()) + 1800
end = start + 3600

r = client.post("/api/card/verify", json={"code": code})
print("verify:", r.status_code, r.get_json())

r = client.post("/api/meeting/create", json={"code": code, "subject": "API测试", "start": start, "end": end})
print("create(ok):", r.status_code, r.get_json())

# 3) 超长拦截（4小时 > 180分钟）
r = client.post("/api/meeting/create", json={"code": code, "subject": "超长", "start": start, "end": start + 4 * 3600})
print("create(too_long):", r.status_code, r.get_json())

# 4) 冲突处理（mock 返回 conflict）
def fake_conflict(subject, start_ts, duration, prefer="web"):
    return {"ok": False, "reason": "conflict", "buffer": 1800, "details": {}}
tm.create_meeting_smart = fake_conflict
r = client.post("/api/meeting/create", json={"code": code, "subject": "冲突", "start": start, "end": end})
print("create(conflict):", r.status_code, r.get_json())

# 5) 额度耗尽（前面已消耗2次，剩1次；这次成功，再请求应 no_quota）
r = client.post("/api/meeting/create", json={"code": code, "subject": "再建", "start": start, "end": end})
print("create(again, ok):", r.status_code, r.get_json())
r = client.post("/api/meeting/create", json={"code": code, "subject": "超额", "start": start, "end": end})
print("create(exhausted):", r.status_code, r.get_json())

print("\n✅ API 接线验证完成")
