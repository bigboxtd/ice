"""
IceHost Hourly KeepAlive
浏览器引擎: CloakBrowser (Playwright 接口，C++ 层指纹伪装，可过 CF Turnstile)
登录策略:
  - 第一次运行（profile 为空）：直接走邮箱密码登录，登录成功后 persistent profile
    自动保存 session cookie + cf_clearance
  - 后续运行（profile 有内容）：先尝试 Cookie 直连服务器页，失败再走邮箱密码登录
  - 登录失败时不保存 profile（workflow 里 profile-check 会检测是否有有效 session）
Profile 持久化: GitHub Actions Cache（不写入 git 历史，公开仓库安全）
代理: Xray SOCKS5 本地代理，透传给 CloakBrowser
"""

import os
import signal
import subprocess
import time

import requests

# ---------------------------------------------------------------------------
# 环境变量
# ---------------------------------------------------------------------------
BASE_URL   = os.getenv("ICEHOST_BASE_URL", "https://dash.icehost.pl")
SERVER_ID  = os.getenv("ICEHOST_SERVER_ID", "").strip()
SERVER_URL = f"{BASE_URL}/server/{SERVER_ID}" if SERVER_ID else None
EMAIL      = os.getenv("ICEHOST_EMAIL")
PASSWORD   = os.getenv("ICEHOST_PASSWORD")
LOGIN_URL  = os.getenv("ICEHOST_LOGIN_URL", f"{BASE_URL}/auth/login")

# persistent_context profile 目录（由 workflow 的 actions/cache 在运行间持久化）
PROFILE_DIR = os.getenv("ICEHOST_PROFILE_DIR", "state/icehost_profile")

# SOCKS5 代理，空字符串 = 直连
PROXY = os.getenv("PROXY", "socks5://127.0.0.1:10808").strip()

# 录屏开关
ENABLE_RECORDING = os.getenv("ENABLE_RECORDING", "false").strip().lower() == "true"
RECORD_FILE      = "icehost_record.mp4"
SCREENSHOT_FILE  = "icehost_debug_screenshot.png"

XVFB_WIDTH  = 1366
XVFB_HEIGHT = 768

# 固定 fingerprint seed：让每次运行看起来像同一台设备（returning visitor）
FINGERPRINT_SEED = os.getenv("ICEHOST_FINGERPRINT_SEED", "54321")

# 登录是否成功的标志，用于 workflow 决定是否保存 profile
LOGIN_SUCCESS = False


# ---------------------------------------------------------------------------
# TG 通知
# ---------------------------------------------------------------------------
def send_tg_notification(message, photo_path=None):
    token   = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        print("未配置 TG 机器人变量，跳过 TG 推送。")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        print("TG 状态通知发送成功。")
    except Exception as e:
        print(f"发送 TG 消息异常: {e}")
    if photo_path and os.path.exists(photo_path):
        try:
            with open(photo_path, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": "IceHost 实时画面"},
                    files={"photo": f},
                    timeout=15,
                )
            print("TG 截图发送成功。")
        except Exception as e:
            print(f"发送 TG 截图异常: {e}")


# ---------------------------------------------------------------------------
# 录屏（ffmpeg x11grab）
# ---------------------------------------------------------------------------
def get_display_resolution(display):
    try:
        out = subprocess.check_output(
            ["xdpyinfo", "-display", display], stderr=subprocess.DEVNULL
        ).decode()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("dimensions:"):
                dims = line.split()[1]
                w, h = dims.split("x")
                return int(w), int(h)
    except Exception as e:
        print(f"探测显示器分辨率失败，使用默认 {XVFB_WIDTH}x{XVFB_HEIGHT}: {e}")
    return XVFB_WIDTH, XVFB_HEIGHT


def start_recording():
    display = os.environ.get("DISPLAY", ":99")
    width, height = get_display_resolution(display)
    print(f"开启录屏，目标显示器: {display}，分辨率: {width}x{height}")
    try:
        proc = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-video_size", f"{width}x{height}",
                "-framerate", "15",
                "-f", "x11grab",
                "-i", display,
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                RECORD_FILE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        return proc
    except Exception as e:
        print(f"启动 ffmpeg 录屏失败，本次跳过录屏: {e}")
        return None


def stop_recording(proc):
    if not proc:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
        print("录屏已正常停止并保存。")
    except Exception as e:
        print(f"停止录屏异常，强制 kill: {e}")
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 截图
# ---------------------------------------------------------------------------
def screenshot(page, path=SCREENSHOT_FILE):
    try:
        page.screenshot(path=path)
    except Exception as e:
        print(f"截图失败: {e}")


# ---------------------------------------------------------------------------
# CF 挑战检测
# ---------------------------------------------------------------------------
def is_cf_challenge(page):
    """
    检测当前页面是否仍处于 CF 整页拦截挑战（而不是嵌入在正常业务页面里的
    隐式/managed Turnstile 小部件，那种小部件常驻在很多页面上做风控打分，
    并不代表页面被拦截）。

    判断逻辑：
      - title 命中 "just a moment" → 一定是拦截页
      - body 命中 CF 专属关键词 → 一定是拦截页
      - 仅仅存在 turnstile/challenges.cloudflare.com 的 iframe，但页面同时
        有大量非 CF 的正常业务文字内容 → 判定为隐式小部件，不算拦截
    """
    has_cf_frame = False
    for frame in page.frames:
        url = frame.url or ""
        if "challenges.cloudflare.com" in url or "turnstile" in url:
            has_cf_frame = True
            print(f"  [CF检测] 发现 turnstile/challenge frame: {url[:80]}")
            break

    try:
        title = page.title()
        if "just a moment" in title.lower():
            print(f"  [CF检测] title 命中: {title}")
            return True
    except Exception:
        pass

    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        pass

    cf_keywords = [
        "Verify you are human",
        "Checking if the site connection is secure",
        "Enable JavaScript and cookies",
    ]
    for kw in cf_keywords:
        if kw in body_text:
            print(f"  [CF检测] body 命中关键词: {kw}")
            return True

    if not has_cf_frame:
        return False

    # 有 turnstile frame，但没有任何 CF 关键词，再看页面正文是否足够丰富。
    # 真正的整页拦截基本不会有超过一两百字的正常业务内容。
    if len(body_text.strip()) > 200:
        print("  [CF检测] 存在 turnstile frame，但页面含大量正常业务内容，判定为隐式小部件（非拦截）。")
        return False

    print("  [CF检测] 存在 turnstile frame 且页面内容稀少，判定为拦截页。")
    return True


# ---------------------------------------------------------------------------
# CF 点击 + 等待通过
# ---------------------------------------------------------------------------
def click_turnstile(page):
    try:
        frames = page.frames
        print(f"当前页面共有 {len(frames)} 个 frame")
        for i, frame in enumerate(frames):
            url = frame.url or ""
            print(f"  frame[{i}] url: {url[:80]}")
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                print(f"  → 找到 Turnstile frame[{i}]，计算坐标...")
                try:
                    elem = frame.frame_element()
                    box  = elem.bounding_box()
                    if box:
                        cx = box["x"] + box["width"]  * 0.12
                        cy = box["y"] + box["height"] * 0.50
                        print(f"  → 点击坐标: ({cx:.1f}, {cy:.1f})")
                        page.mouse.click(cx, cy)
                        return True
                except Exception as fe:
                    print(f"  frame[{i}] 坐标计算异常: {fe}")
    except Exception as e:
        print(f"click_turnstile 异常: {e}")
    print("未找到 Turnstile frame，跳过点击。")
    return False


def wait_for_cf_pass(page, timeout_s=90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not is_cf_challenge(page):
            return True
        time.sleep(2)
    return False


def handle_cf_challenge(page):
    """处理 CF 挑战，最多重试 3 次。返回 True 表示已通过。"""
    for attempt in range(1, 4):
        if not is_cf_challenge(page):
            print(f"CF 挑战已通过（第 {attempt} 次检查）。")
            return True
        print(f"[CF] 第 {attempt}/3 次，尝试点击 Turnstile...")
        click_turnstile(page)
        time.sleep(8)
        if wait_for_cf_pass(page, timeout_s=90):
            print(f"[CF] 第 {attempt} 次点击后验证通过。")
            return True
        print(f"[CF] 第 {attempt}/3 次验证未通过。")
        screenshot(page)
    print("[CF] 3 次重试均失败。")
    return False


# ---------------------------------------------------------------------------
# profile 有效性检测
# ---------------------------------------------------------------------------
def has_valid_profile():
    """
    检查 persistent profile 目录是否存在有效的 Chromium Cookies 文件。
    Cookies 文件存在且大于 8KB（空库约 8KB，有实际 cookie 会更大）才认为有效。
    这样第一次运行（空 profile）或登录失败后（profile 只有 CF 垃圾 cookie）
    都不会走 try_cookie_login，直接走邮箱密码登录。
    """
    cookies_path = os.path.join(PROFILE_DIR, "Default", "Cookies")
    if not os.path.exists(cookies_path):
        print(f"Profile Cookies 文件不存在: {cookies_path}，跳过 Cookie 登录。")
        return False
    size = os.path.getsize(cookies_path)
    print(f"Profile Cookies 文件大小: {size} 字节")
    if size <= 8192:
        print("Cookies 文件过小（可能只有空库或 CF 临时 cookie），跳过 Cookie 登录。")
        return False
    return True


# ---------------------------------------------------------------------------
# Cookie 登录（仅 profile 有实质内容时才尝试）
# ---------------------------------------------------------------------------
def try_cookie_login(page):
    """
    直接访问服务器页，利用 profile 里已有的 session cookie 免登录。
    成功条件：URL 不是登录页 AND 不是 CF 挑战页 AND 页面有足够内容。
    """
    print("尝试使用 persistent profile Cookie 直接访问服务器页面...")
    try:
        page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"Cookie 登录页面加载异常: {e}")

    time.sleep(4)
    screenshot(page)

    cur = page.url
    print(f"当前 URL: {cur}")

    if "auth/login" in cur:
        print("被重定向到登录页，session 已过期。")
        return False

    if is_cf_challenge(page):
        print("当前页面是 CF 挑战页，Cookie 无法绕过 CF，转走邮箱密码登录流程。")
        return False

    try:
        body_text = page.locator("body").inner_text(timeout=3000)
        if len(body_text.strip()) < 50:
            print(f"页面内容过少（{len(body_text.strip())} 字符），视为 Cookie 失效。")
            return False
    except Exception as e:
        print(f"页面内容检查异常: {e}")
        return False

    print(f"Cookie 登录成功！当前 URL: {cur}")
    return True


# ---------------------------------------------------------------------------
# 邮箱密码登录
# ---------------------------------------------------------------------------
def login_with_email_password(page):
    """访问登录页 → 处理 CF 挑战 → 填写表单。成功返回 True。"""
    print(f"[登录] 访问登录页: {LOGIN_URL}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"登录页加载异常（非致命）: {e}")

    time.sleep(5)
    screenshot(page)

    if is_cf_challenge(page):
        print("[登录] 检测到 CF 挑战，开始处理...")
        if not handle_cf_challenge(page):
            send_tg_notification(
                "❌ <b>IceHost CF 验证失败（已重试 3 次）</b>\n"
                "提示：GitHub Actions 数据中心 IP 信誉较低，"
                "建议检查代理是否为住宅 IP，或查看录屏确认实际状态。",
                SCREENSHOT_FILE,
            )
            return False
    else:
        print("[登录] 无 CF 挑战，直接进入登录页。")

    # 如果访问 /auth/login 后被站点自动跳转到了其它页面（说明 session 其实
    # 是有效的，站点认为你已登录，不会再展示用户名/密码输入框），
    # 直接当作登录成功，不要傻等 input[name='username'] 超时。
    if "auth/login" not in page.url:
        print(f"[登录] 访问登录页后被重定向到 {page.url}，说明已处于登录状态，跳过表单步骤。")
        return True

    screenshot(page)
    print("等待登录表单...")
    try:
        page.wait_for_selector("input[name='username']", timeout=10000)
    except Exception as e:
        print(f"登录表单未出现: {e}")
        screenshot(page)
        send_tg_notification("❌ <b>IceHost 登录表单未出现</b>", SCREENSHOT_FILE)
        return False

    print("填写登录表单...")
    try:
        page.fill("input[name='username']", EMAIL)
        page.fill("input[name='password']", PASSWORD)
        screenshot(page)
        page.click("button[type='submit']")
        print("已提交登录表单，等待跳转...")
        time.sleep(5)
    except Exception as e:
        print(f"填写登录表单失败: {e}")
        screenshot(page)
        send_tg_notification("❌ <b>IceHost 登录表单操作失败</b>", SCREENSHOT_FILE)
        return False

    # 提交后可能二次 CF 验证
    if is_cf_challenge(page):
        print("[登录] 提交表单后再次触发 CF 挑战，继续处理...")
        if not handle_cf_challenge(page):
            send_tg_notification("❌ <b>IceHost 登录后 CF 二次验证失败</b>", SCREENSHOT_FILE)
            return False
        time.sleep(3)

    cur = page.url
    if "auth/login" in cur:
        print(f"登录失败，当前 URL: {cur}")
        screenshot(page)
        send_tg_notification("❌ <b>IceHost 登录失败！请检查邮箱/密码。</b>", SCREENSHOT_FILE)
        return False

    print(f"邮箱密码登录成功，当前 URL: {cur}")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run():
    global LOGIN_SUCCESS

    if not SERVER_ID or not EMAIL or not PASSWORD:
        print("错误: 缺少必要环境变量 (ICEHOST_SERVER_ID / ICEHOST_EMAIL / ICEHOST_PASSWORD)")
        return

    from cloakbrowser import launch_persistent_context

    proxy_arg = PROXY if PROXY else None
    if proxy_arg:
        print(f"使用代理: {proxy_arg}")
    else:
        print("未配置 PROXY，使用直连。")

    print(f"Fingerprint seed: {FINGERPRINT_SEED}")
    print(f"Profile 目录: {PROFILE_DIR}")

    os.makedirs(PROFILE_DIR, exist_ok=True)

    recording_proc = None

    context = launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        proxy=proxy_arg,
        geoip=True,
        humanize=True,
        viewport={"width": 1366, "height": 768},
        args=[f"--fingerprint={FINGERPRINT_SEED}"],
    )

    try:
        page = context.new_page()

        if ENABLE_RECORDING:
            recording_proc = start_recording()

        # 1. 判断是否有有效 profile，决定登录路径
        #    has_valid_profile() 为 False 时（首次运行 / 上次登录失败）直接走邮箱密码登录，
        #    不尝试 Cookie 登录，避免用残缺的 CF cookie 去碰服务器页。
        if has_valid_profile():
            logged_in = try_cookie_login(page)
        else:
            print("Profile 无有效 session，直接走邮箱密码登录。")
            logged_in = False

        if not logged_in:
            logged_in = login_with_email_password(page)
            if not logged_in:
                return

        LOGIN_SUCCESS = True

        # 2. 确保在服务器页面
        if SERVER_URL and SERVER_URL not in page.url:
            print(f"访问服务器页面: {SERVER_URL}")
            try:
                page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"服务器页面加载异常: {e}")
            time.sleep(5)

            if is_cf_challenge(page):
                print("访问服务器页面时触发 CF 挑战，处理中...")
                if not handle_cf_challenge(page):
                    send_tg_notification("❌ <b>IceHost 访问服务器页 CF 验证失败</b>", SCREENSHOT_FILE)
                    return

        screenshot(page)
        page_source = page.content()

        # 3. 判定波兰语红框限制
        keywords = ["Nie możesz przedłużyć", "niedawno to zrobiłeś", "kolejne 6 godziny"]
        if any(kw in page_source for kw in keywords):
            print("检测到红框限制提示：未到续期时间，静默退出。")
            return

        # 4. 点击续期按钮
        renew_btn = page.locator(
            "xpath=//*[not(*) and contains("
            "translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')"
            ", 'dodaj 6')]"
        ).first

        try:
            print("等待续期按钮加载...")
            renew_btn.wait_for(state="visible", timeout=15000)
            print("找到续期按钮，点击...")
            renew_btn.click()

            time.sleep(5)
            screenshot(page)
            current_source = page.content()

            if any(kw in current_source for kw in keywords):
                print("点击后出现限制提示：未到可续期时间，静默退出。")
                return

            print("点击后未检测到限制提示，刷新确认结果...")
            page.reload(wait_until="domcontentloaded")
            time.sleep(5)
            screenshot(page)

            updated_source = page.content()
            if any(kw in updated_source for kw in keywords):
                msg = "⚡ <b>IceHost 续期成功！</b>\n服务器已成功延长 6 小时有效期。"
            else:
                msg = "ℹ️ <b>IceHost 续期指令已发送</b>\n请查看截图确认结果。"
            print(msg)
            send_tg_notification(msg, SCREENSHOT_FILE)

        except Exception as e:
            print(f"未找到续期按钮（可能已续满，或按钮文字变动）: {e}")
            screenshot(page)

    finally:
        try:
            context.close()
        except Exception:
            pass
        if recording_proc:
            stop_recording(recording_proc)

        # 登录失败时写入标志文件，让 workflow 跳过 profile 保存
        if not LOGIN_SUCCESS:
            flag_path = "state/icehost_login_failed"
            os.makedirs("state", exist_ok=True)
            with open(flag_path, "w") as f:
                f.write("login_failed")
            print(f"登录失败，已写入标志文件: {flag_path}，本次 profile 不保存。")


if __name__ == "__main__":
    run()
