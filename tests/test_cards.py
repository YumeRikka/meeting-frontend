"""数据层测试：卡密 / 会议记录（SQLite，使用临时库，不污染真实 cards.db）。

锁定核心业务规则：建卡、校验、扣减、过期、记录会议、列表、取消、删除。
这是「密钥管理模块」最容易回归的地方，每次改动后端都应跑。
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("h5", "python"):
    sys.path.insert(0, os.path.join(ROOT, _d))

import cards


class TestCards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cards_test_")
        cards.DB_PATH = os.path.join(self.tmp, "cards.db")
        cards.init_db()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_verify(self):
        code = cards.create_cards(1, quota=3, days_valid=30, max_duration_min=180)[0]
        v = cards.verify(code)
        self.assertTrue(v["ok"])
        self.assertEqual(v["remaining"], 3)
        self.assertEqual(v["quota"], 3)

    def test_consume_reduces_remaining(self):
        code = cards.create_cards(1, quota=3)[0]
        cards.consume(code)
        self.assertEqual(cards.verify(code)["remaining"], 2)

    def test_no_quota(self):
        code = cards.create_cards(1, quota=1)[0]
        cards.consume(code)
        self.assertEqual(cards.verify(code)["reason"], "no_quota")

    def test_expired(self):
        code = cards.create_cards(1, quota=1, days_valid=-1)[0]
        self.assertEqual(cards.verify(code)["reason"], "expired")

    def test_record_and_list_meetings(self):
        code = cards.create_cards(1, quota=5)[0]
        cards.record_meeting(code, "123456789", "主题", "url", "301", "225800", 1000, 2000)
        rows = cards.list_meetings(code)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["meeting_code"], "123456789")
        self.assertEqual(rows[0]["host_key"], "225800")
        self.assertEqual(len(cards.list_meetings()), 1)

    def test_mark_cancelled(self):
        code = cards.create_cards(1, quota=5)[0]
        cards.record_meeting(code, "123456789", "主题", "url", "301", "", 1000, 2000)
        cards.mark_cancelled("123456789")
        self.assertEqual(cards.list_meetings()[0]["status"], "cancelled")

    def test_delete_card(self):
        code = cards.create_cards(1, quota=1)[0]
        self.assertTrue(cards.delete_card(code))
        self.assertFalse(cards.exists(code))
        self.assertFalse(cards.delete_card(code))  # 不存在返回 False


if __name__ == "__main__":
    unittest.main()
