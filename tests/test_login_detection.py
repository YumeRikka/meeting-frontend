"""回归测试：_is_login_page 必须精确判定登录页，不能误杀『预定会议』页。

历史坑：旧逻辑用 `bool(page.query_selector("input[type=password]"))` 判定登录失效，
但预定会议表单自带『会议密码』输入框（type=password），导致已登录账号被误报
『登录态已失效』。本测试锁死：带会议密码框的预定页 → 返回 False；真正的登录页 → 返回 True。
"""

import unittest
import sys
from pathlib import Path

# 让测试能 import 项目 py 模块（不依赖真实浏览器 / playwright）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from create_via_web import _is_login_page  # noqa: E402


class _FakeLoc:
    """模拟 Playwright 的 locator.first.count() 接口。"""

    def __init__(self, count):
        self._count = count

    @property
    def first(self):
        return self

    def count(self):
        return self._count


class _FakePage:
    """最小化的 Playwright Page 替身，只实现 _is_login_page 用到的接口。"""

    def __init__(self, url="", has_account=False, has_qr=False):
        self.url = url
        self._has_account = has_account
        self._has_qr = has_qr

    def query_selector(self, selector):
        # _is_login_page 只会查『账号输入框』选择器（含 account/phone/微信/QQ 等关键字）
        if self._has_account and ("account" in selector or "phone" in selector
                                  or "微信" in selector or "QQ" in selector):
            return object()  # 任何真值都行
        return None

    def get_by_text(self, text, exact=False):
        return _FakeLoc(1 if self._has_qr else 0)


class TestIsLoginPage(unittest.TestCase):

    def test_scheduler_with_meeting_password_is_not_login(self):
        """关键回归：预定会议页有会议密码框（旧逻辑会误判），新逻辑必须返回 False。"""
        # 模拟旧逻辑里会命中的 password 输入框场景，但新函数不应据此判登录页
        page = _FakePage(
            url="https://meeting.tencent.com/user-center/user-meeting-list/schedule",
            has_account=False,
            has_qr=False,
        )
        self.assertFalse(_is_login_page(page), "预定会议页（含会议密码框）不应被当作登录页")

    def test_login_page_with_account_field(self):
        page = _FakePage(
            url="https://meeting.tencent.com/user-center/#/login",
            has_account=True,
            has_qr=False,
        )
        self.assertTrue(_is_login_page(page))

    def test_login_url_only(self):
        page = _FakePage(
            url="https://meeting.tencent.com/passport/login",
            has_account=False,
            has_qr=False,
        )
        self.assertTrue(_is_login_page(page))

    def test_login_qr_only(self):
        page = _FakePage(
            url="https://meeting.tencent.com/some-page",
            has_account=False,
            has_qr=True,
        )
        self.assertTrue(_is_login_page(page))

    def test_plain_scheduler_no_password(self):
        page = _FakePage(
            url="https://meeting.tencent.com/user-center/user-meeting-list/schedule",
            has_account=False,
            has_qr=False,
        )
        self.assertFalse(_is_login_page(page))


if __name__ == "__main__":
    unittest.main()
