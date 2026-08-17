"""会议时长下限回归测试：腾讯会议要求最短 15 分钟，低于此值表单/接口会拒绝。

固化 meeting.tencent.com 表单校验「会议时长最少15分钟」被拦截的坑：
用户传入 1 分钟会议（如 17:00-17:01）时，必须兜底到 15 分钟。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "python"))

import tencent_meeting as tm


class TestDurationFloor(unittest.TestCase):
    def test_below_min_clamped_to_15(self):
        self.assertEqual(tm.normalize_duration_min(1), 15)
        self.assertEqual(tm.normalize_duration_min(0), 15)
        self.assertEqual(tm.normalize_duration_min(14), 15)

    def test_exactly_min_kept(self):
        self.assertEqual(tm.normalize_duration_min(15), 15)

    def test_above_min_unchanged(self):
        self.assertEqual(tm.normalize_duration_min(30), 30)
        self.assertEqual(tm.normalize_duration_min(60), 60)
        self.assertEqual(tm.normalize_duration_min(1440), 1440)

    def test_none_or_string(self):
        self.assertEqual(tm.normalize_duration_min(None), 15)
        self.assertEqual(tm.normalize_duration_min("5"), 15)
        self.assertEqual(tm.normalize_duration_min("20"), 20)

    def test_constant_matches_form_floor(self):
        # create_via_web._set_time 表单层同样按 15 分钟兜底，
        # 这里校验接口层常量与表单层一致，避免两处下限漂移。
        self.assertEqual(tm.MIN_MEETING_DURATION_MIN, 15)


if __name__ == "__main__":
    unittest.main()
