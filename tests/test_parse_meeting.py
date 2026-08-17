"""纯函数测试：会议文本解析（无任何外部依赖，秒级跑完）。

覆盖标签式 / 自由式 / 无时间 / 时长推导等，确保 parse_meeting_text 行为稳定，
避免「前端解析与微信 bot 解析不一致」「时间识别退化」这类静默回归。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("h5", "python"):
    _p = os.path.join(ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from parse_meeting import parse_meeting_text


class TestParseMeetingText(unittest.TestCase):
    def test_labeled_month_day(self):
        r = parse_meeting_text("会议名称：周会\n会议时间：8月16日 18:00-19:00")
        self.assertEqual(r["subject"], "周会")
        self.assertTrue(r["time_found"])
        self.assertEqual(r["duration"], 60)

    def test_labeled_iso(self):
        r = parse_meeting_text("会议名称：项目评审 会议时间：2026-08-20 10:30-11:30")
        self.assertEqual(r["subject"], "项目评审")
        self.assertEqual(r["duration"], 60)
        self.assertTrue(r["time_found"])

    def test_labeled_single_time_defaults_60(self):
        r = parse_meeting_text("会议名称：x 会议时间：8月16日 18:00")
        self.assertEqual(r["duration"], 60)
        self.assertTrue(r["time_found"])

    def test_freeform_tomorrow(self):
        r = parse_meeting_text("英语课 明天 15:00 1小时")
        self.assertIn("英语课", r["subject"])
        self.assertTrue(r["time_found"])
        self.assertEqual(r["duration"], 60)

    def test_freeform_duration_minutes(self):
        r = parse_meeting_text("随便聊聊 14:30 2小时")
        self.assertTrue(r["time_found"])
        self.assertEqual(r["duration"], 120)

    def test_no_time_returns_time_found_false(self):
        r = parse_meeting_text("会议名称：晨会")
        self.assertFalse(r["time_found"])
        self.assertEqual(r["subject"], "晨会")

    def test_empty_returns_none(self):
        self.assertIsNone(parse_meeting_text("   "))
        self.assertIsNone(parse_meeting_text(""))


if __name__ == "__main__":
    unittest.main()
