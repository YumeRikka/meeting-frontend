"""Flask API 集成测试：mock 掉真实浏览器建会，覆盖后端接线与业务规则。

不依赖 playwright / 网络 / 真实腾讯会议账号。前端解析、卡密校验、异步任务、admin 接口全部被验证。
相对路径解析，可跨环境运行（不再硬编码 D:\\WorkBuddy\\...）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("h5", "python"):
    sys.path.insert(0, os.path.join(ROOT, _d))

import cards
import tencent_meeting as tm
import app as flask_app

ADMIN_TOKEN = "TESTADMINTOKEN123"


class TestFlaskAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="flask_test_")
        cards.DB_PATH = os.path.join(cls.tmp, "cards.db")
        cards.init_db()
        flask_app.ADMIN_TOKEN = ADMIN_TOKEN  # 注入测试用管理员令牌
        cls.client = flask_app.app.test_client()
        cls._orig_create = getattr(tm, "create_meeting_smart", None)

    @classmethod
    def tearDownClass(cls):
        if cls._orig_create is not None:
            tm.create_meeting_smart = cls._orig_create
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.start = int(time.time()) + 1800

    def _fake_ok(self, subject, start_ts, duration, prefer="web", on_progress=None):
        return {
            "ok": True,
            "url": "https://meeting.tencent.com/dm/FAKE",
            "code": "123456789",
            "account": "301",
            "userid": "admin1783592634",
            "buffer": 1800,
            "method": "web",
        }

    def _make_card(self, quota=3, max_duration_min=180):
        return cards.create_cards(1, quota=quota, days_valid=30, max_duration_min=max_duration_min)[0]

    # ---- verify ----
    def test_verify_success(self):
        code = self._make_card()
        r = self.client.post("/api/card/verify", json={"code": code})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_verify_not_found(self):
        r = self.client.post("/api/card/verify", json={"code": "NOPE-NOPE-NOPE"})
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(r.get_json()["reason"], "not_found")

    # ---- create 同步校验 ----
    def test_create_missing_card(self):
        r = self.client.post(
            "/api/meeting/create",
            json={"code": "", "subject": "x", "start": self.start, "end": self.start + 3600},
        )
        self.assertEqual(r.get_json()["reason"], "not_found")

    def test_create_too_long(self):
        code = self._make_card(max_duration_min=180)
        r = self.client.post(
            "/api/meeting/create",
            json={"code": code, "subject": "x", "start": self.start, "end": self.start + 4 * 3600},
        )
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(r.get_json()["reason"], "too_long")

    def test_create_end_before_start(self):
        code = self._make_card()
        r = self.client.post(
            "/api/meeting/create",
            json={"code": code, "subject": "x", "start": self.start, "end": self.start - 100},
        )
        self.assertEqual(r.get_json()["reason"], "invalid_params")

    # ---- 异步建会端到端 ----
    def test_create_async_success_writes_meeting(self):
        tm.create_meeting_smart = self._fake_ok
        code = self._make_card()
        r = self.client.post(
            "/api/meeting/create",
            json={"code": code, "subject": "端到端", "start": self.start, "end": self.start + 3600},
        )
        self.assertTrue(r.get_json()["ok"])
        task_id = r.get_json()["task_id"]
        done = self._wait_progress(task_id)
        self.assertIsNotNone(done, "建会任务未在预期时间内完成")
        self.assertTrue(done["ok"], msg=done.get("error"))
        mr = self.client.get(f"/api/meetings?code={code}").get_json()
        self.assertEqual(len(mr["meetings"]), 1)
        self.assertEqual(mr["meetings"][0]["meeting_code"], "123456789")

    # ---- admin 接口 ----
    def test_admin_create_and_delete(self):
        r = self.client.post(
            "/api/admin/cards",
            json={"token": ADMIN_TOKEN, "count": 2, "quota": 5, "max_duration_min": 120},
        )
        self.assertTrue(r.get_json()["ok"])
        codes = r.get_json()["codes"]
        self.assertEqual(len(codes), 2)
        r = self.client.get(f"/api/admin/cards?token={ADMIN_TOKEN}")
        self.assertTrue(r.get_json()["ok"])
        self.assertGreaterEqual(len(r.get_json()["cards"]), 2)
        r = self.client.post("/api/admin/card/delete", json={"token": ADMIN_TOKEN, "code": codes[0]})
        self.assertTrue(r.get_json()["ok"])
        self.assertFalse(cards.exists(codes[0]))

    def test_admin_forbidden_without_token(self):
        r = self.client.post("/api/admin/cards", json={"count": 1, "quota": 1})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["reason"], "forbidden")

    def test_admin_meetings_list(self):
        tm.create_meeting_smart = self._fake_ok
        code = self._make_card()
        r = self.client.post(
            "/api/meeting/create",
            json={"code": code, "subject": "历史", "start": self.start, "end": self.start + 3600},
        )
        self._wait_progress(r.get_json()["task_id"])
        r = self.client.get(f"/api/admin/meetings?token={ADMIN_TOKEN}&code={code}")
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(r.get_json()["meetings"]), 1)

    # ---- 工具 ----
    def _wait_progress(self, task_id, max_polls=50):
        for _ in range(max_polls):
            pr = self.client.get(f"/api/meeting/progress?task={task_id}").get_json()
            if pr.get("done"):
                return pr
            time.sleep(0.1)
        return None


if __name__ == "__main__":
    unittest.main()
