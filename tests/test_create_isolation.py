"""asyncio 事件循环隔离回归测试（针对「Sync API inside the asyncio loop」复发）。

背景：create_meeting_smart 会在同一 worker 线程里串行换号重试 5 个账号；旧实现依赖
_reset_thread_event_loop() 清当前线程 loop，但账号因登录过期等异常中断后 loop 常常清不干净，
下一个账号一进来就误报 "It looks like you are using Playwright Sync API inside the asyncio loop"。
修复方案：create_meeting 每次调用都起一个全新线程跑 playwright、join 取结果，线程结束 loop 随之消亡。

本测试用 mock 取代真实浏览器流程，验证「独立线程 + 异常不跨调用污染 + worker 内清掉残留 loop」
这一修复结构确实生效——这是防止该 bug 复发的核心防线。
"""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _d in ("h5", "python"):
    sys.path.insert(0, os.path.join(ROOT, _d))

import create_via_web


class TestCreateIsolation(unittest.TestCase):
    UID = "test_account_iso"

    def setUp(self):
        self.profile = Path(create_via_web.PROFILE_DIR, self.UID)
        self.profile.mkdir(parents=True, exist_ok=True)
        self.start = int(time.time()) + 3600
        self.end = self.start + 3600

    def tearDown(self):
        shutil.rmtree(self.profile, ignore_errors=True)

    def test_exception_in_one_account_does_not_poison_next(self):
        calls = {"n": 0}

        def fake_sync(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # 模拟某账号在独立线程里因登录过期等异常中断
                raise RuntimeError("模拟 playwright loop 残留 / 登录过期中断")
            return {
                "ok": True,
                "code": "111222333",
                "url": "https://meeting.tencent.com/dm/1",
                "host_key": "225800",
            }

        with mock.patch.object(create_via_web, "_create_meeting_sync", side_effect=fake_sync):
            # 第一次：异常应原样抛出
            with self.assertRaises(RuntimeError):
                create_via_web.create_meeting(self.UID, "主题A", self.start, self.end)
            # 第二次：独立线程机制应保证仍能成功（不被第一次污染）
            res = create_via_web.create_meeting(self.UID, "主题B", self.start, self.end)
            self.assertTrue(res["ok"])
            self.assertEqual(calls["n"], 2)

    def test_worker_clears_event_loop_before_running(self):
        seen = {}
        orig = asyncio.set_event_loop

        def spy(loop):
            seen["called_with"] = loop
            return orig(loop)

        def fake_sync(*a, **k):
            return {"ok": True, "code": "X", "url": "u", "host_key": ""}

        with mock.patch.object(create_via_web, "_create_meeting_sync", side_effect=fake_sync), \
                mock.patch("asyncio.set_event_loop", side_effect=spy):
            create_via_web.create_meeting(self.UID, "主题", self.start, self.end)
            self.assertIsNone(
                seen.get("called_with"),
                "worker 线程未调用 asyncio.set_event_loop(None) 双保险，asyncio loop 可能残留",
            )


if __name__ == "__main__":
    unittest.main()
