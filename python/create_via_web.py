# create_via_web.py
# 通过驱动「腾讯会议网页端」(meeting.tencent.com) 登录账号来创建会议，
# 从而绕过企业 REST API 的「创建会议」月度限频（12 次/月）。
#
# 关键点：
#   - 查询空闲账号仍走 REST GET（不限频），见 tencent_meeting.py
#   - 真正建会走这里（网页登录），可设会议名称 + 主持人密钥，且不占创建配额
#   - 登录态持久化到 profiles/{userid}/ 浏览器配置目录（原生持久化，像真实 Chrome 一样
#     记住登录），首次需 --login 一次性交互登录；之后建会复用同一目录，不再请求验证码
#   - 若短信验证码被腾讯限频（「操作过于频繁」），可用 --qr 走微信扫码登录，彻底避开短信
#
# 凭证来源：本地 accounts.json（必须 git 忽略，切勿提交或在聊天中发送密码）
# 依赖：pip install playwright && playwright install chromium
import os
import sys
import json
import re
import time
import asyncio
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

BASE = Path(__file__).resolve().parent
COOKIE_DIR = BASE / "cookies"          # 仅作「已登录」标记文件，真正状态在 profiles/
COOKIE_DIR.mkdir(exist_ok=True)
PROFILE_DIR = BASE / "profiles"        # 每个账号一个持久化浏览器目录
PROFILE_DIR.mkdir(exist_ok=True)

# 让本文件被单独执行（如 `docker exec ... python create_via_web.py --login xxx`）时
# 也能读到 /app/python/.env 里的腾讯会议 REST 凭证，否则建会会在凭证检查处失败。
try:
    from dotenv import load_dotenv
    load_dotenv(str(BASE / ".env"))
except ImportError:
    pass

WEB_URL = "https://meeting.tencent.com/"

# 无头 Chromium 在容器/受限环境下常因沙箱、/dev/shm 或 GPU 崩溃，统一加这些启动参数
CHROME_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

# 建会进度步骤（索引即上报给前端的 step），前端据此读条
PROGRESS_STEPS = [
    "正在准备浏览器与登录态…",   # 0
    "正在打开预定会议页面…",      # 1
    "正在设置主持人密钥…",        # 2
    "已填写会议主题",             # 3
    "正在设置会议时间…",          # 4
    "正在提交并创建会议…",        # 5
    "正在获取会议信息…",          # 6
]
PROGRESS_TOTAL = len(PROGRESS_STEPS)


def _state_path(userid):
    return COOKIE_DIR / f"{userid}.json"


def load_accounts(path=None):
    """读取 accounts.json：{"accounts":[{"name","userid","account","password"}]}，
    返回 {userid: {...}}。account=登录标识(手机/邮箱)，password=网页登录密码。"""
    p = Path(path) if path else (BASE / "accounts.json")
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    accs = data.get("accounts", []) if isinstance(data, dict) else data
    return {a["userid"]: a for a in accs if a.get("userid")}


def save_state(context, userid):
    """持久化上下文已通过 profiles/{userid}/ 自动保存登录态；
    这里额外导出一份 storage_state 仅作冗余标记，失败（如沙箱无写权限）直接忽略，
    绝不能让它中断建会流程或触发 REST 回退。"""
    try:
        context.storage_state(path=str(_state_path(userid)))
    except Exception:
        pass


def _require_pw():
    if sync_playwright is None:
        raise RuntimeError("未安装 playwright，请先执行：pip install playwright && playwright install chromium")


def _safe_accept(dialog):
    """自动接受原生对话框（alert/confirm/beforeunload），避免无头模式卡死。"""
    try:
        dialog.accept()
    except Exception:
        pass


def _fmt_dt(ts):
    """时间戳 -> 网页 datetime-local 需要的 'YYYY-MM-DDTHH:MM'。"""
    d = datetime.fromtimestamp(int(ts))
    return d.strftime("%Y-%m-%dT%H:%M")


# 表单里能标识「预定会议」页面的选择器（真实占位符：请输入会议名称）
_SUBJECT_SEL = (
    "input[placeholder='请输入会议名称'], "
    "input[placeholder*='会议名称'], input[placeholder*='会议主题'], "
    "input[placeholder*='subject'], input[name='subject'], textarea[placeholder*='主题']"
)


def _pick_scheduler_page(ctx):
    """在 ctx 所有页面里挑出「预定会议」表单页。
    优先按 _SUBJECT_SEL（有主题输入框），其次按 URL 含 /schedule。"""
    for pg in ctx.pages:
        try:
            if pg.query_selector(_SUBJECT_SEL):
                return pg
        except Exception:
            continue
    # 兜底：URL 含 /schedule 就是表单页（React SPA 可能渲染慢）
    for pg in ctx.pages:
        try:
            if "/schedule" in pg.url:
                # 再等一下 React 渲染出输入框
                pg.wait_for_timeout(2000)
                if pg.query_selector("input") or pg.query_selector("select"):
                    return pg
        except Exception:
            continue
    return None


def _js_click(page, text, exact=False):
    """点击含指定文字的元素。优先用 JS 直接触发（对腾讯会议 <li> 包裹 <span> 的导航最稳，
    之前实测能打开「发起会议」下拉）；若 JS 未命中或无效，再退回 Playwright force 真实点击。
    """
    js = r"""
    (args) => {
        const want = args[0], exact = args[1];
        const all = [...document.querySelectorAll('a,li,button,span,div,label')];
        const matched = all.filter(e => {
            const t = (e.textContent || '').trim();
            return exact ? t === want : t.includes(want);
        });
        // 优先点击「可点击容器」(a/li/button)，避免点到内部 <span> 导致父级 handler 不响应
        const clickable = matched.filter(e => /^(A|LI|BUTTON)$/.test(e.tagName));
        const pool = clickable.length ? clickable : matched;
        // 取可见的、文本最短的那个（最具体的一层）
        pool.sort((a, b) => (a.textContent || '').length - (b.textContent || '').length);
        // 派发完整鼠标事件序列（mousedown/mouseup/click），并连父元素一起触发，
        // 以兼容「监听 mousedown」或「事件挂在父级容器」的下拉项（如腾讯会议 meeting-schedule）
        const fire = (node) => {
            ['mousedown', 'mouseup', 'click'].forEach(type => {
                node.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
            });
        };
        for (const e of pool) {
            const r = e.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {
                fire(e);
                if (e.parentElement) fire(e.parentElement);
                return true;
            }
        }
        return false;
    }
    """
    try:
        if bool(page.evaluate(js, [text, exact])):
            return True
    except Exception:
        pass
    # 兜底：Playwright 真实点击（force 绕过父级拦截）
    try:
        page.get_by_text(text, exact=exact).first.click(force=True, timeout=8000)
        return True
    except Exception:
        return False


def _open_scheduler(ctx, page):
    """从首页进入预定会议表单，返回表单所在页面。
    流程：点「发起会议」-> 下拉出现 -> 点「预定会议」-> 等待表单页（可能在原页，也可能在新标签）。"""
    try:
        page.wait_for_selector("li.nav-item, a, button", timeout=15000)
    except Exception:
        raise RuntimeError("页面未正常加载（未检测到导航元素），登录态可能已失效，请重新 --login")
    if not _js_click(page, "发起会议"):
        raise RuntimeError("未找到「发起会议」，登录态可能已失效，请重新 --login")
    page.wait_for_timeout(1800)
    # 先尝试在出现的下拉里点「预定会议」（已知其结构为 <div id="meeting-schedule">，故按 id 精确点击最稳）
    try:
        page.evaluate(
            "() => { const el = document.getElementById('meeting-schedule'); "
            "if (!el) return false; "
            "['mousedown','mouseup','click'].forEach(t => el.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); "
            "if (el.parentElement) ['mousedown','mouseup','click'].forEach(t => el.parentElement.dispatchEvent(new MouseEvent(t,{bubbles:true,cancelable:true,view:window}))); "
            "return true; }"
        )
    except Exception:
        _js_click(page, "预定会议")
    # 等待表单页出现：最多 15 秒，每 1 秒扫一次；中途若还没出现，再补点一次「预定会议」
    sched = None
    for i in range(15):
        sched = _pick_scheduler_page(ctx)
        if sched is not None:
            break
        if i == 4:
            _js_click(page, "预定会议")  # 下拉可能已收起，补点一次
        page.wait_for_timeout(1000)
    if sched is None:
        # 还没找到，再等 5 秒给新标签加载时间
        page.wait_for_timeout(5000)
        sched = _pick_scheduler_page(ctx)
    if sched is None:
        raise RuntimeError(
            "未能定位到会议表单页面。\n"
            "可能原因：① 点「预定会议」后新标签未打开  ② 表单选择器不匹配\n"
            f"当前所有标签 URL: {[p.url for p in ctx.pages]}\n"
            "请贴出 inspect 截图或 PNG"
        )
    return sched


def _is_login_page(page):
    """精确判定当前是否处于腾讯会议『登录页』。

    历史坑：之前用 `bool(page.query_selector("input[type=password]"))` 判断登录失效，
    但『预定会议』表单页本身就带一个「会议密码」输入框（type=password），导致即使已登录
    也会被误判为「登录态已失效」。这里改用登录页的强特征来判定，避免误杀：
      - URL 为登录/通行证路径（/login、passport、sign-in）
      - 存在账号输入框（手机号/邮箱/微信/QQ 登录）
      - 存在「扫码登录 / 二维码登录」入口
    只要命中其一即视为登录页；否则视为已登录（即便页面上有会议密码框）。
    """
    try:
        url = (page.url or "").lower()
        if "/login" in url or "passport" in url or "sign-in" in url:
            return True
        # 会议密码框也是 type=password，但绝不会和「账号登录输入框」共存于同一页。
        has_account_field = bool(page.query_selector(
            'input[name="account"], input[name="u"], input[name="phone"], '
            'input[placeholder*="手机号"], input[placeholder*="邮箱"], '
            'input[placeholder*="账号"], input[placeholder*="微信"], input[placeholder*="QQ"]'
        ))
        if has_account_field:
            return True
        try:
            has_qr = bool(page.get_by_text("扫码登录", exact=False).first.count()) or \
                bool(page.get_by_text("二维码登录", exact=False).first.count())
        except Exception:
            has_qr = False
        if has_qr:
            return True
        return False
    except Exception:
        return False


# ---------- 一次性交互登录 ----------
def login_once(userid, account=None, password=None, headless=False, qr=False):
    """打开可见浏览器完成登录，保存登录态到 profiles/{userid}/（持久化，之后建会复用，不再请求验证码）。
    qr=True 时走微信扫码登录，彻底避开短信验证码（适合短信被限频的场景）。
    """
    _require_pw()
    creds = load_accounts().get(userid, {})
    account = account or creds.get("account") or userid
    password = password or creds.get("password")
    if not qr and not (account and password):
        raise RuntimeError(f"账号 {userid} 缺少登录账号/密码（请在 accounts.json 填 account 与 password）")

    profile = str(PROFILE_DIR / userid)
    with sync_playwright() as p:
        # 关键：用持久化用户目录，登录态原生保留，后续建会无需重复登录/验证码
        # 关键：用持久化用户目录，登录态原生保留，后续建会无需重复登录/验证码
        ctx = p.chromium.launch_persistent_context(profile, headless=headless, args=CHROME_ARGS)
        page = ctx.new_page()
        page.goto(WEB_URL, wait_until="domcontentloaded", timeout=60000)
        try:
            page.get_by_text("登录", exact=True).first.click(timeout=15000)
        except Exception:
            pass

        if qr:
            scanned = False
            for label in ["扫码登录", "二维码登录", "微信扫码", "二维码"]:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=4000)
                    scanned = True
                    break
                except Exception:
                    continue
            if not scanned:
                print("[login] 未找到扫码入口，请手动在页面点「扫码登录」后用微信扫码。")
            print("[login] 请用手机微信扫描页面二维码完成登录。")
            try:
                page.get_by_text("预定会议", exact=False).first.wait_for(timeout=120000)
            except Exception:
                raise RuntimeError("等待扫码登录超时，请重试。")
        else:
            try:
                page.get_by_text("账号密码登录", exact=True).click(timeout=8000)
            except Exception:
                pass  # 有的版本默认就是账号密码登录
            page.fill(
                'input[placeholder*="手机号"], input[placeholder*="邮箱"], input[placeholder*="账号"], input[name="account"]',
                account,
            )
            page.fill('input[type="password"]', password)
            page.get_by_text("登录", exact=True).click(timeout=8000)
            print("[login] 若页面出现短信验证码，请在浏览器中手动完成。")
            print("[login] 重要：验证码请只点一次「发送」，收到后立刻填写，避免触发腾讯「操作过于频繁」。")
            try:
                page.get_by_text("预定会议", exact=False).first.wait_for(timeout=60000)
            except Exception:
                raise RuntimeError("登录后未进入主界面（可能登录失败或需要验证），请重试。")

        # 一旦进入主界面立即落盘（标记文件 + user_data_dir 已自动持久化），
        # 这样即使你直接关掉浏览器窗口，登录态也已经保存在 profiles/{userid}/ 里。
        save_state(ctx, userid)
        print(f"[login] 已保存登录态 -> 浏览器配置目录 {profile}")
        input("登录态已保存，按回车关闭浏览器（或直接关窗口均可）：")
        ctx.close()
    return True


# ---------- 建会 ----------
def _set_date_input(sched, inp, date_str):
    """用日历弹层点选设置日期（受控 React 组件，自由键入文本不提交，必须走日历点选）。
    date_str 形如 'YYYY/MM/DD'。"""
    import re as _re
    m = _re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})", date_str or "")
    if not m:
        print(f"[time] 日期格式异常: {date_str!r}，跳过日期设置")
        return
    ty, tm, td = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        inp.click(timeout=5000)  # 打开日历 portal
        sched.wait_for_timeout(700)
        cal = sched.locator(".met-calendar").first
        if cal.count() == 0:
            raise RuntimeError("未找到 .met-calendar 日历弹层")
        # 翻到目标年月
        for _ in range(24):
            title = cal.evaluate(
                """(el) => {
                    const t = (el.innerText || '').match(/(\\d{4})年\\s*(\\d{1,2})月/);
                    return t ? {y: +t[1], m: +t[2]} : null;
                }"""
            )
            if not title:
                break
            if title["y"] == ty and title["m"] == tm:
                break
            if (ty > title["y"]) or (ty == title["y"] and tm > title["m"]):
                nav = sched.locator(
                    "xpath=//a[contains(@class,'met-pagination__turnbtn')]"
                    "[.//i[contains(@class,'arrow-right-light')]]"
                ).first
            else:
                nav = sched.locator(
                    "xpath=//a[contains(@class,'met-pagination__turnbtn')]"
                    "[.//i[contains(@class,'arrow-left-light')]]"
                ).first
            if nav.count() == 0:
                break
            nav.click(timeout=3000)
            sched.wait_for_timeout(300)
        # 点选目标日：当前月、未禁用、且非相邻月溢出(--ou)
        cells = cal.locator(".met-calendar__cell")
        n = cells.count()
        clicked = False
        for i in range(n):
            cell = cells.nth(i)
            cls = (cell.get_attribute("class") or "")
            txt = (cell.inner_text() or "").strip()
            if txt == str(td) and "is-disabled" not in cls and "--ou" not in cls:
                cell.click(timeout=3000)
                clicked = True
                break
        sched.wait_for_timeout(300)
        if not clicked:
            raise RuntimeError(f"日历中未找到可选的目标日 {td}（{date_str}）")
        print(f"[time] 日历已选日期 {date_str}")
        return
    except Exception as e:
        print(f"[time] 日历设置日期失败，回退 JS setter: {e}")
    # 兜底：JS setter（通常不更新 React state，仅作最后尝试）
    try:
        inp.evaluate(
            """(el, val) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            date_str,
        )
    except Exception as e:
        print(f"[time] JS setter 设置日期也失败: {e}")


def _pick_closest_time(sched, picker_loc, hh, mm):
    """在已展开的时间列表里选 HH:MM（兜底用，仅当直接赋值输入框无效时调用）。
    优先精确匹配；若列表（如虚拟滚动仅渲染部分项）没有该分钟，则就近取整后点击，
    返回 (h, m, snapped)，snapped=True 表示发生了取整。"""
    # 入参可能是字符串（来自 strftime），统一转 int，避免 diff() 里出现 str 运算
    hh = int(hh)
    mm = int(mm)
    list_loc = picker_loc.locator("div.list")
    items = list_loc.locator("li.MetTimePicker_metTimePickerList__Eq1es")
    n = items.count()
    if n == 0:
        raise RuntimeError("时间列表为空，无法选择时间")
    cands = []
    for i in range(n):
        it = items.nth(i)
        try:
            h = int((it.locator("input.hour").get_attribute("value") or -1))
            m = int((it.locator("input.minute").get_attribute("value") or -1))
        except Exception:
            h = m = -1
        cands.append((h, m, i))
    # 1) 精确匹配
    for (h, m, i) in cands:
        if h == hh and m == mm:
            items.nth(i).click(force=True)
            return h, m, False
    # 2) 就近取整（列表只提供部分档位时）
    valid = [c for c in cands if c[0] >= 0]
    if not valid:
        raise RuntimeError(f"时间列表无可选项，无法选择 {hh}:{mm}")
    def diff(h, m):
        return abs((h * 60 + m) - (hh * 60 + mm))
    valid.sort(key=lambda c: diff(c[0], c[1]))
    h, m, i = valid[0]
    items.nth(i).click(force=True)
    return h, m, True


def _set_time_picker(sched, picker_loc, hh, mm):
    """在自定义时间选择器（<section class="custom-time-picker">）里选 HH:MM，支持任意分钟。
    优先直接给选择器自身的 input.hour/input.minute 赋值（对应官网“可直接输入每分钟”的交互，
    不依赖下拉列表渲染），若直接赋值无效则回退到列表精确点击（必要时就近取整）。"""
    hh = int(hh)
    mm = int(mm)
    # 1) 打开下拉：优先点 caret 图标（.symbol 容器），保证输入框/列表交互就绪
    try:
        caret = picker_loc.locator(".symbol, .met-icon-caret-down--filled")
        if caret.count() > 0:
            caret.first.click(force=True, timeout=4000)
        else:
            picker_loc.click(force=True, timeout=4000)
    except Exception:
        try:
            picker_loc.click(force=True, timeout=4000)
        except Exception:
            pass
    sched.wait_for_timeout(400)
    # 2) 优先：真实键盘输入 hour/minute（与主题框同一套 —— 只有真实输入事件才能把值
    #    持久写进 React state；JS setter / 直接改 input.value 只改 DOM，提交前被重渲染冲掉）。
    #    关键：每设完一个选择器必须 Escape 关掉它的下拉，否则它悬在下层选择器之上，
    #    下一次点击会落空（开始时间停在默认 00:00 就是这个原因）。
    try:
        sched.keyboard.press("Escape")  # 关掉上一个可能还开着的下拉
        sched.wait_for_timeout(150)
        hIn = picker_loc.locator("input.hour").first
        mIn = picker_loc.locator("input.minute").first
        if hIn.count() > 0 and mIn.count() > 0:
            hIn.click(timeout=3000)
            sched.keyboard.press("Control+a")
            sched.keyboard.type(f"{hh:02d}", delay=20)
            mIn.click(timeout=3000)
            sched.keyboard.press("Control+a")
            sched.keyboard.type(f"{mm:02d}", delay=20)
            sched.wait_for_timeout(200)
            hv = hIn.input_value()
            mv = mIn.input_value()
            if hv == f"{hh:02d}" and mv == f"{mm:02d}":
                sched.keyboard.press("Escape")  # 关掉本选择器下拉
                return hh, mm, False
            print(f"[time] 真实键盘设置时间未生效（h={hv} m={mv}），回退列表点击")
    except Exception as e:
        print(f"[time] 真实键盘设置时间异常，回退列表点击: {e}")
    # 3) 回退：点击下拉列表项（列表仅有步进档位，必要时就近取整）
    try:
        h, m, snapped = _pick_closest_time(sched, picker_loc, hh, mm)
        sched.wait_for_timeout(200)
        sched.keyboard.press("Escape")  # 关掉下拉
        if not snapped:
            return hh, mm, False
        print(f"[time] 列表无精确档位，已就近取整 {hh}:{mm}→{h}:{m}")
        return h, m, True
    except Exception as e:
        print(f"[time] 列表点击设置时间也失败: {e}")
    return hh, mm, False


def _set_time(sched, start_ts, end_ts, on_progress=None):
    """设置开始/结束时间。腾讯会议网页端真实控件（已用 --inspect 校准）：
      日期：<input placeholder="选择日期" value="YYYY/MM/DD">（可文本填写）
      时间：<section class="custom-time-picker"> 自定义选择器，支持任意分钟
            （优先精确匹配；列表为虚拟滚动时自动滚动查找，仅极少见无该档位才就近取整）。
    不再强制对齐到 :00/:30；仅做「不能早于当前时间」与「结束晚于开始」的兜底校正。"""
    if on_progress:
        on_progress(4, PROGRESS_STEPS[4])

    now = int(time.time())
    MIN_DURATION = 15 * 60  # 腾讯会议最短会议时长 15 分钟
    # 兜底：不允许开始时间早于当前（腾讯会报「时间不能早于当前时间」）
    if int(start_ts) <= now:
        start_ts = now + 60
    # 结束必须晚于开始，且时长不低于腾讯下限 15 分钟
    if int(end_ts) <= int(start_ts):
        end_ts = int(start_ts) + MIN_DURATION
    elif int(end_ts) - int(start_ts) < MIN_DURATION:
        end_ts = int(start_ts) + MIN_DURATION

    start_dt = datetime.fromtimestamp(int(start_ts))
    end_dt = datetime.fromtimestamp(int(end_ts))

    s_date = start_dt.strftime("%Y/%m/%d")
    e_date = end_dt.strftime("%Y/%m/%d")
    s_hh, s_mm = start_dt.strftime("%H"), start_dt.strftime("%M")
    e_hh, e_mm = end_dt.strftime("%H"), end_dt.strftime("%M")

    # ---- 日期 ----
    date_inputs = sched.locator('input[placeholder="选择日期"]')
    if date_inputs.count() >= 2:
        _set_date_input(sched, date_inputs.nth(0), s_date)
        _set_date_input(sched, date_inputs.nth(1), e_date)
    elif date_inputs.count() == 1:
        _set_date_input(sched, date_inputs.first, s_date)
    sched.wait_for_timeout(400)

    # ---- 时间（自定义选择器）----
    start_picker = sched.locator(".start .custom-time-picker").first
    end_picker = sched.locator(".end .custom-time-picker").first
    if start_picker.count() > 0 and end_picker.count() > 0:
        snapped = []
        # 先设开始、后设结束：设开始时没有任何下拉遮挡，开始输入框可干净点击落定；
        # 结束最后设，其下拉开着也无妨（值已提交进 state）。
        h, m, snap = _set_time_picker(sched, start_picker, s_hh, s_mm)
        if snap:
            snapped.append(f"{s_hh}:{s_mm}→{h}:{m}")
        h, m, snap = _set_time_picker(sched, end_picker, e_hh, e_mm)
        if snap:
            snapped.append(f"{e_hh}:{e_mm}→{h}:{m}")
        if snapped:
            print(f"[time] 部分分钟列表无对应档位，已就近取整：{snapped}")
        print(f"[time] 已设时间 {s_date} {s_hh}:{s_mm} - {e_hh}:{e_mm}")
    else:
        raise RuntimeError(
            "未找到时间选择器（.custom-time-picker），请运行 --inspect 并贴出截图校准。"
        )
        if on_progress:
            msg = "已设置会议时间"
            if snapped:
                msg += "（部分分钟已就近取整到可选档位）"
            on_progress(4, msg)


def _close_playwright_nonblocking(p, ctx):
    """非阻塞关闭浏览器 context：放到守护线程里关 ctx.close()，最多等 2 秒；
    超时则放弃，绝不阻塞主线程返回结果。playwright driver 的 p.stop() 由
    _nonblocking_playwright 的 __exit__ 统一非阻塞处理。"""
    import threading

    def _do():
        try:
            ctx.close()
        except Exception:
            pass

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=2)  # 最多等 2 秒；超时则放弃，绝不阻塞主线程


def _stop_playwright_nonblocking(p):
    """非阻塞停止 playwright driver：守护线程 + 最多等 2 秒，绝不阻塞主线程。
    浏览器进程若仍不响应，由 OS 回收，不再卡住结果返回。"""
    import threading

    def _do():
        try:
            p.stop()
        except Exception:
            pass

    t = threading.Thread(target=_do, daemon=True)
    t.start()
    t.join(timeout=2)


def _reset_thread_event_loop():
    """卸载 sync_playwright().start() 在当前线程安装的 asyncio 事件循环。

    sync_playwright().start() 会在「调用它的线程」里 new 一个事件循环并 set 为当前线程的 loop。
    而 _stop_playwright_nonblocking 把 p.stop() 丢到**另一个守护线程**执行，导致该 loop 没在
    「当前线程」被正确卸载。若同一线程随后再调用 sync_playwright().start()（例如 create_meeting_smart
    里换账号重试），asyncio.get_event_loop().is_running() 仍为真 → 误报
    "It looks like you are using Playwright Sync API inside the asyncio loop"。

    这里在当前线程显式把它关掉、置空，保证同线程下一次 start() 能新建一个干净的 loop。"""
    try:
        loop = asyncio.get_event_loop()
    except Exception:
        loop = None
    if loop is not None:
        try:
            if loop.is_running():
                loop.stop()
        except Exception:
            pass
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


@contextmanager
def _nonblocking_playwright():
    """替代 `with sync_playwright() as p:`：退出时非阻塞关闭，绝不因浏览器进程
    不响应而卡死。关键：把 p.stop() 放到守护线程带超时执行，避免 `with` 原生的
    p.stop() 在浏览器进程不响应时阻塞数十秒（表现为「结果页已抓取成功却还等很久」）。
    退出后显式卸载当前线程的 asyncio 事件循环，避免同线程换账号重试时误报
    "Sync API inside the asyncio loop"。"""
    p = sync_playwright().start()
    try:
        yield p
    finally:
        _stop_playwright_nonblocking(p)
        _reset_thread_event_loop()


def _scrape_result(page):
    """从创建成功结果页里抓会议号 + 入会链接 + 主持人密钥。
    返回 (code, url, host_key)。抓不到抛 RuntimeError（交由上层 REST 兜底）。
    注意：结果页会议号带空格（如「598 521 134」），链接在 <span class="decrypt-text"> 而非 <a href>，
    故用「按 label 定位字段 → 取 .decrypt-text/.met-form__text 文本 → 去非数字」的稳健方式解析。"""
    import re
    try:
        page.wait_for_selector("text=会议号", timeout=8000)  # 成功页才出现「会议号」字段
    except Exception:
        pass
    page.wait_for_timeout(800)  # 给渲染兜底
    data = page.evaluate(
        """() => {
            const getVal = (labelText) => {
                const labels = [...document.querySelectorAll('label')];
                for (const l of labels) {
                    if ((l.textContent || '').trim().includes(labelText)) {
                        const item = l.closest('.met-form__item') || l.closest('.form-item');
                        if (!item) continue;
                        const t = item.querySelector('.decrypt-text') || item.querySelector('.met-form__text');
                        if (t) return (t.innerText || t.textContent || '').trim();
                    }
                }
                const dt = document.querySelector('.decrypt-text');
                return dt ? (dt.innerText || dt.textContent || '').trim() : '';
            };
            return {
                codeRaw: getVal('会议号'),
                linkRaw: getVal('会议链接'),
                keyRaw: getVal('主持人密钥')
            };
        }"""
    )
    code_raw = data.get("codeRaw", "") or ""
    code = re.sub(r"\D", "", code_raw)  # 「598 521 134」 -> 「598521134」
    if not (9 <= len(code) <= 11):
        code = ""
    link_raw = data.get("linkRaw", "") or ""
    m = re.search(r"https?://meeting\.tencent\.com/[^\s'\"<>]+", link_raw)
    url = m.group(0) if m else ""
    if not url:
        # 兜底：扫 <a href> 与页面全文
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if "meeting.tencent.com" in href and ("/dm/" in href or "/p/" in href):
                url = href
                break
    if not url:
        body = page.inner_text("body") or ""
        m2 = re.search(r"https?://meeting\.tencent\.com/[^\s'\"<>]+", body)
        url = m2.group(0) if m2 else ""
    host_key = re.sub(r"\D", "", data.get("keyRaw", "") or "")
    if not code or not url:
        # 兜底：dump 一段页面文本方便校准
        raise RuntimeError(
            f"已点击提交但未解析出会议号/链接。code_raw={code_raw!r} link_raw={link_raw!r} code={code!r} url={url!r}"
        )
    return code, url, host_key



def create_meeting(userid, subject, start_ts, end_ts, host_key="", headless=True, on_progress=None):
    """对外入口：在独立的干净线程里跑 playwright 建会，彻底杜绝
    「同一线程反复 sync_playwright().start() 触发 asyncio 事件循环泄漏」的报错
    （It looks like you are using Playwright Sync API inside the asyncio loop）。

    背景：create_meeting_smart 会在同一个 worker 线程里串行换号重试 5 个账号；
    旧实现依赖 _reset_thread_event_loop() 清当前线程的 loop，但账号因登录过期等异常
    中断后，loop 常常清不干净，下一个账号一进来就误报「Sync API inside the asyncio loop」。
    现在每次调用都新建一个线程，线程结束时其事件循环随之消亡，账号之间零共享、零污染。"""
    import threading
    _require_pw()
    profile = PROFILE_DIR / userid
    if not profile.exists():
        raise RuntimeError(f"账号 {userid} 尚未登录，请先运行：python create_via_web.py --login {userid}")
    if on_progress:
        on_progress(0, PROGRESS_STEPS[0])

    box, err = {}, {}

    def _worker():
        try:
            # 本线程从未跑过 playwright，天生无残留 loop；再显式清一遍双保险
            try:
                asyncio.set_event_loop(None)
            except Exception:
                pass
            box["v"] = _create_meeting_sync(
                userid, subject, start_ts, end_ts, host_key=host_key,
                headless=headless, on_progress=on_progress,
            )
        except Exception as e:  # noqa: BLE001
            err["e"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "e" in err:
        raise err["e"]
    return box["v"]


def _create_meeting_sync(userid, subject, start_ts, end_ts, host_key="", headless=True, on_progress=None):
    """建会核心逻辑（同步，在 create_meeting 的独立线程中执行）。"""
    profile = PROFILE_DIR / userid
    if not profile.exists():
        raise RuntimeError(f"账号 {userid} 尚未登录，请先运行：python create_via_web.py --login {userid}")

    with _nonblocking_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=headless, args=CHROME_ARGS)
        # 自动接受任何原生对话框（alert/confirm/beforeunload），避免无头模式下卡死
        ctx.on("dialog", lambda d: _safe_accept(d))
        page = ctx.new_page()
        page.goto(WEB_URL, wait_until="domcontentloaded", timeout=60000)
        sched = _open_scheduler(ctx, page)
        if on_progress:
            on_progress(1, PROGRESS_STEPS[1])

        # ---- 取消勾选「允许成员在主持人进会前加入会议」（默认勾选，需主动关掉）----
        try:
            label = sched.get_by_text("允许成员在主持人进会前加入会议", exact=False).first
            if label.count() > 0:
                # 真实控件：父级 <label class="met-form-check met-form-check--active"> 表示已勾选
                is_checked = label.evaluate(
                    """(el) => {
                        const box = el.closest('label.met-form-check') || el.closest('.met-form-check');
                        if (!box) return false;
                        return box.classList.contains('met-form-check--active') ||
                               box.classList.contains('is-checked');
                    }"""
                )
                if is_checked:
                    label.click(timeout=5000)
                    print("[create] 已取消勾选「允许成员在主持人进会前加入会议」")
                else:
                    print("[create] 「允许成员在主持人进会前加入会议」本就未勾选，跳过")
            else:
                print("[warn] 未找到「允许成员在主持人进会前加入会议」复选框，跳过")
        except Exception as e:
            print(f"[warn] 处理「允许成员在主持人进会前加入会议」失败（跳过）: {e}")

        # ---- 主持人密钥：勾选「开启密钥」复选框（点击 label 切换，原生 input 是 readonly），再填值 ----
        if on_progress:
            on_progress(2, PROGRESS_STEPS[2])
        if host_key:
            try:
                chk_label = sched.get_by_text("开启密钥", exact=False).first
                chk_label.click(timeout=5000)
                sched.wait_for_timeout(800)
                # 填密钥值（真实占位符：请输入6位数字密钥）
                sched.fill('input[placeholder="请输入6位数字密钥"]', host_key)
                print(f"[create] 主持人密钥: {host_key}")
            except Exception as e:
                print(f"[warn] 设置主持人密钥失败（跳过）: {e}")

        # ---- 会议主题（最后一步填！前面的时间/勾选/密钥交互会触发 React 重渲染，
        #      若在它们之前填主题，重渲染会用空 state 把主题输入框清空 → 报「会议主题不能全为空格」。
        #      用 press_sequentially 逐字符真实输入，确保 React onChange 真正写入 state。）----
        if on_progress:
            on_progress(3, PROGRESS_STEPS[3])
        subj_sel = 'input[placeholder="请输入会议名称"]'
        try:
            sched.wait_for_selector(subj_sel, timeout=30000)
        except Exception:
            # 走到这里说明已过 _open_scheduler 的登录态检测，通常是页面加载慢（尤其 NAS 冷启动）；
            # 若实际是登录态失效跳转到了登录页，给出明确提示。
            try:
                on_login_page = _is_login_page(page)
            except Exception:
                on_login_page = False
            if on_login_page:
                raise RuntimeError(
                    f"账号 {userid} 登录态已失效，请重新登录（运行 create_via_web.py --login {userid}）")
            raise

        def _fill_subject_robust(val):
            # 腾讯 met-input 的 onChange 源码：H.current ? J(t) : x(t) ——只有在「非输入法合成中」
            # (H.current=false) 时才会走 x(t) 真正把值提交到 React state。任何合成事件（compositionstart）
            # 都会把 H.current 置 true，导致值只进临时变量 J(t)、不提交，提交时 state 仍空 → 「不能全为空格」。
            # 因此这里用「真实键盘逐字输入」（不触发 composition，H.current 保持 false），让 onChange 走 x(t)。
            loc = sched.locator(subj_sel)
            loc.click(timeout=5000)
            sched.keyboard.press("Control+a")   # 全选（含腾讯预填的默认主题「预定的会议」）
            sched.keyboard.type(val, delay=20)  # 真实逐字键入，逐个字符触发原生 input → React onChange → x(t)
            return sched.evaluate(
                "(args) => { const el = document.querySelector(args[0]); return el ? el.value : ''; }",
                [subj_sel],
            )

        cur = ""
        for _try in range(3):
            res = _fill_subject_robust(subject)
            sched.wait_for_timeout(500)
            cur = sched.evaluate(
                "(args) => { const el = document.querySelector(args[0]); return el ? el.value : ''; }",
                [subj_sel],
            )
            print(f"[create] 主题回填结果={res!r} 当前值={cur!r}")
            if cur and cur.strip():
                break
            print(f"[warn] 主题第 {_try + 1} 次回填仍为空（React state 疑似未更新），重试…")
        print(f"[create] 主题实际值: {cur!r} (期望 {subject!r})")
        if not cur or not str(cur).strip():
            raise RuntimeError(
                f"会议主题填写失败（实际值={cur!r}），主题输入框占位符/结构可能已变，请运行 --inspect 校准"
            )

        # ---- 会议时间（放在最后填！取消勾选/密钥/主题交互会触发 React 重渲染，
        #      把已写入 state 的时间值冲掉；故时间必须最后落定、提交前不再有任何交互）----
        if on_progress:
            on_progress(4, PROGRESS_STEPS[4])
        _set_time(sched, start_ts, end_ts, on_progress=on_progress)

        # ---- 提交：「预定会议」按钮（精确 class 定位 button.meeting-button-area-confirm）----
        if on_progress:
            on_progress(5, PROGRESS_STEPS[5])
        submit_sel = "button.meeting-button-area-confirm"
        # 先点一下主题输入框做 blur，确保时间/下拉的 state 已提交；再滚动按钮入视图非 force 点击
        try:
            sched.locator('input[placeholder="请输入会议名称"]').first.click(timeout=3000)
            sched.wait_for_timeout(150)
        except Exception:
            pass
        btn = sched.query_selector(submit_sel)
        clicked = False
        if btn is not None:
            for _attempt in range(3):
                try:
                    btn.scroll_into_view_if_needed(timeout=3000)
                    sched.wait_for_timeout(200)
                    btn.click(timeout=8000)  # 非 force：自动滚动+可达性校验，真实命中按钮
                    clicked = True
                    break
                except Exception as e:
                    print(f"[warn] 第 {_attempt+1} 次点击提交按钮失败: {e}")
                    sched.wait_for_timeout(500)
        if not clicked:
            if not _js_click(sched, "预定会议"):
                raise RuntimeError("未找到「预定会议」提交按钮，请贴出 inspect 截图")
        print("[create] 已点击「预定会议」，等待结果…")

        # ---- 处理可能的「会议冲突提示」弹窗 ----
        # 正常情况：REST 已预检并挑选无冲突账号，此弹窗不应出现。
        # 若仍出现（竞态 / REST 未预检），【不要】强行「仍然预定」去创建重叠会议，
        # 而是干净上报「时段冲突」，避免生成重复会议（与「先查已有会议避免冲突」的诉求一致）。
        sched.wait_for_timeout(2000)
        # 注意：腾讯会议的「仍然预定」按钮作为弹窗模板常驻 DOM（隐藏态），
        # get_by_text 会把它也算进去 → 永远误判冲突。必须检查【真实可见性】
        # （包围盒 >0 且 display/visibility/opacity 均可见），与本项目「校验须看可见性」一致。
        conflict_diag = sched.evaluate(
            """() => {
                const els = [...document.querySelectorAll('button,a,span,div,li')];
                const out = [];
                for (const e of els) {
                    const t = (e.textContent || '').trim();
                    if (t.includes('仍然预定')) {
                        const r = e.getBoundingClientRect();
                        const cs = getComputedStyle(e);
                        const parent = e.closest('[class*="modal"],[class*="dialog"],[class*="popup"],[class*="overlay"]') || e.parentElement;
                        const pcs = parent ? getComputedStyle(parent) : null;
                        out.push({
                            text: t,
                            rect: [Math.round(r.width), Math.round(r.height)],
                            offsetParent: e.offsetParent !== null,
                            display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
                            parentDisplay: pcs ? pcs.display : null,
                            parentVisibility: pcs ? pcs.visibility : null,
                            parentOpacity: pcs ? pcs.opacity : null,
                        });
                    }
                }
                return out;
            }"""
        )
        print(f"[conflict] 诊断: {conflict_diag}")
        has_conflict = any(
            d["rect"][0] > 0 and d["rect"][1] > 0
            and d["display"] != "none" and d["visibility"] != "hidden" and float(d["opacity"]) > 0
            and (d["parentDisplay"] is None or d["parentDisplay"] != "none")
            and (d["parentVisibility"] is None or d["parentVisibility"] != "hidden")
            and (d["parentOpacity"] is None or float(d["parentOpacity"]) > 0)
            for d in conflict_diag
        )
        if has_conflict:
            subj = ""
            try:
                subj = sched.evaluate(
                    "() => { const e=document.querySelector('input[placeholder=\"请输入会议名称\"]'); return e ? e.value : ''; }"
                ) or ""
            except Exception:
                subj = ""
            raise RuntimeError(
                f"时段冲突：账号 {userid} 在请求的会议时段已有会议（弹出「会议冲突提示」）。"
                f"已跳过强行创建，避免生成重叠会议。请更换时间或账号后重试。主题={subj!r}"
            )

        # ---- 抓取结果：在所有已打开标签里找会议号（成功弹窗可能新开标签）----
        if on_progress:
            on_progress(6, PROGRESS_STEPS[6])
        code, url = "", ""
        candidates = [sched] + [pg for pg in ctx.pages if pg != sched]
        for pg in candidates:
            try:
                c, u, hk = _scrape_result(pg)
                if c and u:
                    code, url = c, u
                    print(f"[create] 结果页抓取成功：会议号={code} 链接={url}" + (f" 密钥={hk}" if hk else ""))
                    break
            except Exception as e:
                print(f"[create] 结果页抓取失败（{e}），尝试其它标签/REST 兜底")
                continue
        if not (code and url):
            # 判断是「校验未通过（停在表单）」还是「已提交但结果页无头崩溃抓不到」
            still_form = False
            try:
                still_form = sched.query_selector(submit_sel) is not None
            except Exception:
                still_form = False
            if still_form:
                info = []
                for i, pg in enumerate(ctx.pages):
                    try:
                        u = pg.evaluate("() => location.href")
                    except Exception:
                        u = "(无法读取)"
                    try:
                        t = pg.evaluate("() => document.body.innerText || ''")
                    except Exception:
                        t = ""
                    # 抽取可能的校验错误文本（只读可见的，忽略静态隐藏的错误图标 title）
                    err = ""
                    try:
                        err = pg.evaluate(
                            """() => {
                                for (const tip of document.querySelectorAll('.tea-action-state__text')) {
                                    const hidden = (tip.closest('.hide-error-tips') !== null) || (tip.offsetParent === null);
                                    if (!hidden) { const v = (tip.getAttribute('title') || tip.innerText || '').trim(); if (v) return v; }
                                }
                                return '';
                            }"""
                        ) or ""
                    except Exception:
                        err = ""
                    info.append(f"[标签{i}] url={u}\n  文本片段={t[:200]!r}")
                    if err:
                        info.append(f"  ⚠ 校验错误: {err[:200]!r}")
                # 抓主题/时间字段实际值，便于定位是哪个字段没填进去
                try:
                    fields = sched.evaluate(
                        """() => {
                            const out = {};
                            const s = document.querySelector('input[placeholder=\"请输入会议名称\"]');
                            out.subject = s ? s.value : '<无主题框>';
                            const hp = document.querySelector('.start .custom-time-picker input.hour');
                            const mp = document.querySelector('.start .custom-time-picker input.minute');
                            const he = document.querySelector('.end .custom-time-picker input.hour');
                            const me = document.querySelector('.end .custom-time-picker input.minute');
                            out.start = (hp && mp) ? (hp.value + ':' + mp.value) : '<无起始时间>';
                            out.end = (he && me) ? (he.value + ':' + me.value) : '<无结束时间>';
                            return JSON.stringify(out);
                        }"""
                    )
                    if fields:
                        info.append(f"  🔍 字段值: {fields}")
                except Exception:
                    pass
                # 诊断：提交按钮是否被遮挡 / 页面有无校验错误文本
                try:
                    diag = sched.evaluate(
                        """() => {
                            const out = {};
                            const btn = document.querySelector('button.meeting-button-area-confirm');
                            if (btn) {
                                const r = btn.getBoundingClientRect();
                                const cx = r.left + r.width/2, cy = r.top + r.height/2;
                                const top = document.elementFromPoint(cx, cy);
                                out.btnRect = [Math.round(r.width), Math.round(r.height)];
                                out.topAtBtn = top ? (top.tagName + '.' + (top.className||'').toString().slice(0,40)) : 'null';
                                out.btnIsTop = (top === btn) || (btn.contains(top));
                                out.btnDisabled = btn.disabled;
                            } else {
                                out.btn = 'missing';
                            }
                            // 扫描可见的错误/提示文本
                            const errs = [];
                            const walk = (el) => {
                                if (el.children.length === 0) {
                                    const t = (el.textContent||'').trim();
                                    if (t && (el.offsetParent !== null)) errs.push(t.slice(0,60));
                                } else { for (const c of el.children) walk(c); }
                            };
                            walk(document.body);
                            out.visibleTexts = errs.filter(t => /(错误|不能|早于|晚于|冲突|必填|请|无效|格式)/.test(t)).slice(0,8);
                            return JSON.stringify(out);
                        }"""
                    )
                    info.append(f"  🔧 诊断: {diag}")
                except Exception as e:
                    info.append(f"  🔧 诊断失败: {e}")
                raise RuntimeError(
                    "点击「预定会议」后仍停留在表单页，疑似被校验拦截。\n各标签状态：\n"
                    + "\n".join(info)
                )
            # 表单已提交（提交按钮消失），但无头环境结果页可能崩溃无法抓取
            # → 交由上层用 REST 反查会议号/链接，避免重复建会
            print("[create] 表单已提交，结果页未抓取，将交由上层 REST 反查会议号")
        save_state(ctx, userid)  # 续期登录态
        # 非阻塞关闭（沙箱里 ctx.close 可能挂死，绝不阻塞返回结果）
        _close_playwright_nonblocking(p, ctx)
    return {"ok": True, "code": code, "url": url, "host_key": host_key}


# ---------- 检查模式：抓取「预定会议」表单真实 DOM 以便校准选择器 ----------
def inspect_meeting_dialog(userid, headless=False):
    """打开已登录账号，进入「预定会议」表单，导出所有输入框/按钮信息 + 截图 + 页面 HTML
    到 inspect_{userid}.png / inspect_{userid}.html，供校准 create_meeting 的选择器。"""
    _require_pw()
    profile = PROFILE_DIR / userid
    if not profile.exists():
        raise RuntimeError(f"账号 {userid} 尚未登录，请先运行：python create_via_web.py --login {userid}")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=headless, args=CHROME_ARGS)
        page = ctx.new_page()
        page.goto(WEB_URL, wait_until="domcontentloaded", timeout=60000)
        sched = _open_scheduler(ctx, page)

        print("=== 当前 URL (表单页) ===")
        print(sched.url)
        print("\n=== INPUT / TEXTAREA / SELECT ===")
        lines = []
        lines.append("URL: " + sched.url)
        lines.append("=== INPUT / TEXTAREA / SELECT ===")
        for el in sched.query_selector_all("input, textarea, select"):
            tag = el.evaluate("e => e.tagName")
            ph = el.get_attribute("placeholder") or ""
            tp = el.get_attribute("type") or ""
            name = el.get_attribute("name") or ""
            aid = el.get_attribute("aria-label") or ""
            cls = (el.get_attribute("class") or "")[:70]
            line = f"  <{tag}> type={tp!r} name={name!r} aria={aid!r} placeholder={ph!r} class={cls!r}"
            print(line)
            lines.append(line)

        print("\n=== BUTTONS ===")
        lines.append("=== BUTTONS ===")
        for el in sched.query_selector_all("button"):
            txt = (el.inner_text() or "").strip()
            if txt:
                print(f"  <button> {txt!r}")
                lines.append(f"  <button> {txt!r}")

        shot = BASE / f"inspect_{userid}.png"
        sched.screenshot(path=str(shot), full_page=False)
        print(f"\n[inspect] 截图 -> {shot}")
        hpath = BASE / f"inspect_{userid}.html"
        hpath.write_text(sched.content(), encoding="utf-8")
        print(f"[inspect] 页面 HTML -> {hpath}")
        spath = BASE / f"inspect_{userid}_summary.txt"
        spath.write_text("\n".join(lines), encoding="utf-8")
        print(f"[inspect] 摘要 -> {spath}")
        input("按回车关闭浏览器（或直接关窗口）：")
        ctx.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--login":
        uid = sys.argv[2] if len(sys.argv) > 2 else None
        if not uid:
            print("用法: python create_via_web.py --login <userid> [--qr]")
            sys.exit(1)
        qr = "--qr" in sys.argv
        login_once(uid, headless=False, qr=qr)
    elif len(sys.argv) > 1 and sys.argv[1] == "--inspect":
        uid = sys.argv[2] if len(sys.argv) > 2 else None
        if not uid:
            print("用法: python create_via_web.py --inspect <userid>")
            sys.exit(1)
        inspect_meeting_dialog(uid, headless=False)
    elif len(sys.argv) > 1 and sys.argv[1] == "--test":
        # 快速建会测试：用 wemeeting8280568 创建一个 10 分钟后的测试会议
        import time as _time
        uid = sys.argv[2] if len(sys.argv) > 2 else "wemeeting8280568"
        now = int(_time.time())
        start_ts = now + 1800  # 30 分钟后（留出余量，避免对齐后贴着当前时间被拒）
        end_ts = start_ts + 3600  # 1 小时时长
        subject = f"自动化测试会议-{int(now)}"
        print(f"[test] 账号={uid} 主题={subject}")
        print(f"[test] 时间：{datetime.fromtimestamp(start_ts).strftime('%Y/%m/%d %H:%M')} - {datetime.fromtimestamp(end_ts).strftime('%H:%M')}")
        result = create_meeting(uid, subject, start_ts, end_ts, host_key="225800", headless=False)
        print(f"\n[test] ✅ 建会成功！")
        print(f"  会议号：{result['code']}")
        print(f"  入会链接：{result['url']}")
        print(f"  主持人密钥：{result['host_key']}")
    else:
        print("用法:")
        print("  python create_via_web.py --login <userid> [--qr]     # 登录（持久化）")
        print("  python create_via_web.py --inspect <userid>          # 检查表单结构（调试用）")
        print("  python create_via_web.py --test [userid]             # 建会测试（默认 wemeeting8280568）")
