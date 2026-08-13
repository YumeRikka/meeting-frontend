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
from pathlib import Path
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

BASE = Path(__file__).resolve().parent
COOKIE_DIR = BASE / "cookies"          # 仅作「已登录」标记文件，真正状态在 profiles/
COOKIE_DIR.mkdir(exist_ok=True)
PROFILE_DIR = BASE / "profiles"        # 每个账号一个持久化浏览器目录
PROFILE_DIR.mkdir(exist_ok=True)
WEB_URL = "https://meeting.tencent.com/"

# 无头 Chromium 在容器/受限环境下常因沙箱、/dev/shm 或 GPU 崩溃，统一加这些启动参数
CHROME_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]

# 建会进度步骤（索引即上报给前端的 step），前端据此读条
PROGRESS_STEPS = [
    "正在准备浏览器与登录态…",   # 0
    "正在打开预定会议页面…",      # 1
    "正在设置会议时间…",          # 2
    "正在设置主持人密钥…",        # 3
    "已填写会议主题",             # 4
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
    """React 兼容地设置日期文本输入框（placeholder="选择日期"，显示 YYYY/MM/DD）。"""
    inp.evaluate(
        """(el, val) => {
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        date_str,
    )


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
    # 2) 优先：直接给选择器自身的 hour/minute 输入框赋值（任意分钟）
    try:
        res = picker_loc.evaluate(
            """(el, args) => {
                const hh = args[0], mm = args[1];
                const hIn = el.querySelector('input.hour');
                const mIn = el.querySelector('input.minute');
                if (!hIn || !mIn) return 'no-input';
                const setVal = (inp, v) => {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(inp, v);
                    inp.dispatchEvent(new Event('input', { bubbles: true }));
                    inp.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const H = String(hh).padStart(2, '0'), M = String(mm).padStart(2, '0');
                setVal(hIn, H); setVal(mIn, M);
                return (hIn.value === H && mIn.value === M) ? 'ok' : 'mismatch';
            }""",
            [hh, mm],
        )
        if res == 'ok':
            sched.wait_for_timeout(200)
            return hh, mm, False
        print(f"[time] 直接赋值输入框未生效（{res}），回退列表点击")
    except Exception as e:
        print(f"[time] 直接赋值输入框异常，回退列表点击: {e}")
    # 3) 回退：列表精确点击（必要时就近取整）
    h, m, snapped = _pick_closest_time(sched, picker_loc, hh, mm)
    sched.wait_for_timeout(300)
    return h, m, snapped


def _set_time(sched, start_ts, end_ts, on_progress=None):
    """设置开始/结束时间。腾讯会议网页端真实控件（已用 --inspect 校准）：
      日期：<input placeholder="选择日期" value="YYYY/MM/DD">（可文本填写）
      时间：<section class="custom-time-picker"> 自定义选择器，支持任意分钟
            （优先精确匹配；列表为虚拟滚动时自动滚动查找，仅极少见无该档位才就近取整）。
    不再强制对齐到 :00/:30；仅做「不能早于当前时间」与「结束晚于开始」的兜底校正。"""
    if on_progress:
        on_progress(2, PROGRESS_STEPS[2])

    now = int(time.time())
    # 兜底：不允许开始时间早于当前（腾讯会报「时间不能早于当前时间」）
    if int(start_ts) <= now:
        start_ts = now + 60
    if int(end_ts) <= int(start_ts):
        end_ts = int(start_ts) + 60

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
            on_progress(2, msg)


def _scrape_result(page):
    """从创建成功弹窗里抓会议号 + 入会链接。"""
    import re
    page.wait_for_timeout(3000)  # 等成功弹窗渲染（无头模式可能稍慢）
    txt = page.inner_text("body")
    m = re.search(r"(?:会议号[：:\s]*|)\b(\d{9,11})\b", txt)
    code = m.group(1) if m else ""
    # 入会链接
    url = ""
    for a in page.query_selector_all("a[href]"):
        href = a.get_attribute("href") or ""
        if "meeting.tencent.com" in href and ("/dm/" in href or "/p/" in href):
            url = href
            break
    if not url:
        m2 = re.search(r"(https?://meeting\.tencent\.com/[^\s'\"<>]+)", txt)
        url = m2.group(1) if m2 else ""
    if not code or not url:
        # 兜底：dump 一段页面文本方便校准
        raise RuntimeError(f"已点击提交但未解析出会议号/链接。页面片段：\n{txt[:1000]}")
    return code, url


def create_meeting(userid, subject, start_ts, end_ts, host_key="", headless=True, on_progress=None):
    """登录指定账号并创建会议，返回 {"code","url","host_key"}。
    复用 profiles/{userid}/ 持久化登录态（首次需 --login）。登录态失效会提示重新登录。
    on_progress(index, message=None): 进度回调，index 对应 PROGRESS_STEPS。"""
    _require_pw()
    profile = PROFILE_DIR / userid
    if not profile.exists():
        raise RuntimeError(f"账号 {userid} 尚未登录，请先运行：python create_via_web.py --login {userid}")

    if on_progress:
        on_progress(0, PROGRESS_STEPS[0])

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(profile), headless=headless, args=CHROME_ARGS)
        # 自动接受任何原生对话框（alert/confirm/beforeunload），避免无头模式下卡死
        ctx.on("dialog", lambda d: _safe_accept(d))
        page = ctx.new_page()
        page.goto(WEB_URL, wait_until="domcontentloaded", timeout=60000)
        sched = _open_scheduler(ctx, page)
        if on_progress:
            on_progress(1, PROGRESS_STEPS[1])
        # ---- 会议时间 ----
        _set_time(sched, start_ts, end_ts, on_progress=on_progress)

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
            on_progress(3, PROGRESS_STEPS[3])
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
            on_progress(4, PROGRESS_STEPS[4])
        subj_sel = 'input[placeholder="请输入会议名称"]'
        sched.wait_for_selector(subj_sel, timeout=10000)

        def _fill_subject_robust(val):
            loc = sched.locator(subj_sel)
            try:
                loc.click(timeout=5000)
                loc.fill("")
                loc.press_sequentially(val, delay=25)
            except Exception as e:
                print(f"[warn] subject press_sequentially 失败，改用原生 setter 兜底: {e}")
                try:
                    sched.evaluate(
                        """(args) => {
                            const sel = args[0], v = args[1];
                            const el = document.querySelector(sel);
                            if (!el) return;
                            const proto = Object.getPrototypeOf(el);
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                            setter.call(el, v);
                            if (el._valueTracker) {
                                try { el._valueTracker.setValue((el.value || '') + '\\u200b'); } catch (e2) {}
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }""",
                        [subj_sel, val],
                    )
                except Exception as e2:
                    print(f"[warn] subject 原生 setter 兜底也失败: {e2}")
            return sched.evaluate(
                "(args) => { const el = document.querySelector(args[0]); return el ? el.value : ''; }",
                [subj_sel],
            )

        cur = ""
        for _try in range(3):
            cur = _fill_subject_robust(subject)
            sched.wait_for_timeout(300)
            cur = sched.evaluate(
                "(args) => { const el = document.querySelector(args[0]); return el ? el.value : ''; }",
                [subj_sel],
            )
            if cur and cur.strip():
                break
            print(f"[warn] 主题第 {_try + 1} 次回填仍为空，重试…")
        print(f"[create] 主题实际值: {cur!r} (期望 {subject!r})")
        if not cur or not str(cur).strip():
            raise RuntimeError(
                f"会议主题填写失败（实际值={cur!r}），主题输入框占位符/结构可能已变，请运行 --inspect 校准"
            )

        # ---- 提交：「预定会议」按钮（精确 class 定位 button.meeting-button-area-confirm）----
        if on_progress:
            on_progress(5, PROGRESS_STEPS[5])
        submit_sel = "button.meeting-button-area-confirm"
        btn = sched.query_selector(submit_sel)
        clicked = False
        if btn is not None:
            try:
                btn.click(force=True, timeout=8000)
                clicked = True
            except Exception as e:
                print(f"[warn] 按 class 点击提交按钮失败，改用文字点击兜底: {e}")
        if not clicked:
            if not _js_click(sched, "预定会议"):
                raise RuntimeError("未找到「预定会议」提交按钮，请贴出 inspect 截图")
        print("[create] 已点击「预定会议」，等待结果…")

        # ---- 抓取结果：在所有已打开标签里找会议号（成功弹窗可能新开标签）----
        if on_progress:
            on_progress(6, PROGRESS_STEPS[6])
        code, url = "", ""
        candidates = [sched] + [pg for pg in ctx.pages if pg != sched]
        for pg in candidates:
            try:
                c, u = _scrape_result(pg)
                if c and u:
                    code, url = c, u
                    break
            except Exception:
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
                    # 抽取可能的校验错误文本
                    err = ""
                    try:
                        err = pg.evaluate(
                            "() => { const e = document.querySelector("
                            "'.ant-form-item-explain-error, [class*=\"error\"], [class*=\"Error\"]'); "
                            "return e ? (e.innerText || '').trim() : ''; }"
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
                raise RuntimeError(
                    "点击「预定会议」后仍停留在表单页，疑似被校验拦截。\n各标签状态：\n"
                    + "\n".join(info)
                )
            # 表单已提交（提交按钮消失），但无头环境结果页可能崩溃无法抓取
            # → 交由上层用 REST 反查会议号/链接，避免重复建会
            print("[create] 表单已提交，结果页未抓取，将交由上层 REST 反查会议号")
        save_state(ctx, userid)  # 续期登录态
        try:
            ctx.close()
        except Exception:
            pass
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
