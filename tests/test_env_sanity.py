"""环境自检测试：防止再次出现「未安装 playwright」误报。

只检查「能否 import playwright」与「chromium 浏览器二进制是否存在」，
不真正启动浏览器，秒级完成。若哪天 .venv 被搞坏或浏览器被清掉，此测试立刻变红，
比等到线上建会失败再排查快得多。
"""
import importlib.util
import os
import sys
import unittest


class TestEnvSanity(unittest.TestCase):
    def test_playwright_importable(self):
        spec = importlib.util.find_spec("playwright")
        self.assertIsNotNone(
            spec,
            "playwright 未安装 → 会导致 '未安装 playwright' 误报；请执行 "
            "pip install playwright && playwright install chromium",
        )

    def test_chromium_binary_present(self):
        local = os.environ.get("LOCALAPPDATA", "")
        self.assertTrue(local, "环境变量 LOCALAPPDATA 未设置，无法确定 playwright 浏览器缓存位置")
        base = os.path.join(local, "ms-playwright")
        self.assertTrue(os.path.isdir(base), f"未找到 playwright 浏览器缓存目录: {base}")
        chromium_dirs = [d for d in os.listdir(base) if d.startswith("chromium")]
        self.assertTrue(
            chromium_dirs,
            f"ms-playwright 下无 chromium-* 目录（浏览器二进制未安装）: {base}",
        )


if __name__ == "__main__":
    unittest.main()
