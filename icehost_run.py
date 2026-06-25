"""
IceHost Hourly KeepAlive
浏览器引擎: CloakBrowser (Playwright 接口，C++ 层指纹伪装，可过 CF Turnstile)
登录策略: Cookie 优先，失效后邮箱密码登录，登录成功后自动更新 Cookie 文件
Cookie 持久化: GitHub Actions Cache（不写入 git 历史，公开仓库安全）
代理: Xray SOCKS5 本地代理，透传给 CloakBrowser
"""

import json
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

# Cookie 文件路径（由 workflow 的 actions/cache 在运行间持久化）
COOKIE_FILE = os.getenv("ICEHOST_COOKIE_FILE", "state/icehost_cookies.json")

# SOCKS5 代理，空字符串 = 直连
PROXY = os.getenv("PROXY", "socks5://127.0.0.1:10808").strip()

# 录屏开关（来自 workflow_dispatch 下拉）
ENABLE_RECORDING = os.getenv("ENABLE_RECORDING", "false").strip().lower() == "true"
RECORD_FILE      = "icehost_record.mp4"
SCREENSHOT_FILE  = "icehost_debug_screenshot.png"

# 录屏兜底分辨率
XVFB_WIDTH  = 1366
XVFB_HEIGHT = 768


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
# Cookie 持久化
# ---------------------------------------------------------------------------
def load_saved_cookies():
    if not os.path.exists(COOKIE_FILE):
        print(f"未找到本地 Cookie 文件（{COOKIE_FILE}），跳过 Cookie 登录。")
        return None
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            print(f"已读取 {len(data)} 个 Cookie。")
            return data
    except Exception as e:
        print(f"读取 Cookie 文件失败: {e}")
    return None


def save_cookies(context):
    """将 Playwright BrowserContext 的 storage_state 里的 cookies 写入文件"""
    try:
        state = context.storage_state()
        cookies = state.get("cookies", [])
        os.makedirs(os.path.dirname(COOKIE_FILE) or ".", exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"已保存 {len(cookies)} 个 Cookie 到 {COOKIE_FILE}。")
    except Exception as e:
        print(f"保存 Cookie 失败（不影响本次结果）: {e}")


def inject_cookies(context, cookies):
    """向 Playwright BrowserContext 注入 Cookie 列表"""
    valid = []
    for c in cookies:
        try:
            entry = {
                "name":   c["name"],
                "value":  c["value"],
                "domain": c.get("domain", ""),
                "path":   c.get("path", "/"),
            }
            # Playwright cookie 的 sameSite 取值: "Strict" | "Lax" | "None"
            ss = str(c.get("sameSite", "")).capitalize()
            if ss in ("Strict", "Lax", "None"):
                entry["sameSite"] = ss
            if c.get("secure"):
                entry["secure"] = True
            if c.get("expires") and c["expires"] > 0:
                entry["expires"] = c["expires"]
            valid.append(entry)
        except Exception as e:
            print(f"跳过无效 Cookie {c.get('name')}: {e}")
    if valid:
        context.add_cookies(valid)
        print(f"成功注入 {len(valid)}/{len(cookies)} 个 Cookie。")


# ---------------------------------------------------------------------------
# 截图（Playwright page）
# ---------------------------------------------------------------------------
def screenshot(page, path=SCREENSHOT_FILE):
    try:
        page.screenshot(path=path)
    except Exception as e:
        print(f"截图失败: {e}")


# ---------------------------------------------------------------------------
# CF 验证 + 登录（CloakBrowser / Playwright）
# ---------------------------------------------------------------------------
def wait_for_login_page(page, timeout_s=40):
    """等待登录表单出现，返回 True / False"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            page.wait_for_selector("input[name='username']", timeout=2000)
            return True
        except Exception:
            pass
        # 也检查 URL 不再是 CF challenge
        if "auth/login" in page.url and "cf_chl" not in page.url:
            try:
                page.wait_for_selector("input[name='username']", timeout=3000)
                return True
            except Exception:
                pass
    return False


def click_turnstile(page):
    """
    在 Playwright page 里枚举所有 frames，找到 CF Turnstile iframe，
    用 frame_element().bounding_box() 取坐标后用 page.mouse.click() 点击。
    与 Zytrano/justrunmy 同款方案。
    """
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
                        # checkbox 在 iframe 左侧约 12% 处，垂直居中
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


def login_with_email_password(page, context):
    """
    CF 验证 + 邮箱密码登录。
    CloakBrowser 的 C++ 指纹伪装让 CF 更容易自动通过，
    如果没自动过则用 frame 枚举精准点击 Turnstile checkbox，最多重试 3 次。
    成功返回 True。
    """
    for attempt in range(1, 4):
        print(f"[登录] 第 {attempt}/3 次尝试，访问: {LOGIN_URL}")
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"页面加载异常（非致命）: {e}")

        # 等几秒让 CF JS 执行
        time.sleep(4)
        screenshot(page)

        # 情况 1：CF 自动通过，直接进了登录表单
        try:
            page.wait_for_selector("input[name='username']", timeout=3000)
            print("CF 自动通过，已进入登录页。")
            break
        except Exception:
            pass

        print("仍在 CF 验证页，尝试点击 Turnstile checkbox...")
        click_turnstile(page)

        # 点击后最多等 35 秒等 CF 放行
        if wait_for_login_page(page, timeout_s=35):
            print(f"第 {attempt} 次 CF 验证通过，已进入登录页。")
            break
        else:
            print(f"第 {attempt}/3 次 CF 验证未通过。")
            screenshot(page)
            if attempt == 3:
                send_tg_notification(
                    "❌ <b>IceHost CF 验证失败（已重试 3 次）</b>", SCREENSHOT_FILE
                )
                return False

    # 填写登录表单
    print("填写登录表单...")
    screenshot(page)
    try:
        page.wait_for_selector("input[name='username']", timeout=10000)
        # 用 humanize-style 逐字符输入，降低自动化特征
        page.fill("input[name='username']", EMAIL)
        page.fill("input[name='password']", PASSWORD)
        screenshot(page)
        page.click("button[type='submit']")
        print("已提交登录表单，等待跳转...")
        time.sleep(5)
    except Exception as e:
        print(f"填写登录表单失败: {e}")
        screenshot(page)
        send_tg_notification(
            "❌ <b>IceHost 登录表单操作失败</b>", SCREENSHOT_FILE
        )
        return False

    # 确认登录成功
    cur = page.url
    if "auth/login" in cur:
        print(f"登录失败，当前 URL: {cur}")
        screenshot(page)
        send_tg_notification(
            "❌ <b>IceHost 登录失败！请检查邮箱/密码。</b>", SCREENSHOT_FILE
        )
        return False

    print(f"邮箱密码登录成功，当前 URL: {cur}")
    save_cookies(context)
    return True


def try_cookie_login(page, context):
    """Cookie 优先登录，失效返回 False"""
    saved = load_saved_cookies()
    if not saved:
        return False

    print("注入已保存 Cookie，尝试直接访问服务器页面...")
    inject_cookies(context, saved)

    try:
        page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"Cookie 登录页面加载异常: {e}")

    time.sleep(4)
    screenshot(page)

    cur = page.url
    if "auth/login" in cur:
        print("Cookie 已失效，改用邮箱密码登录。")
        return False

    print(f"Cookie 登录成功！当前 URL: {cur}")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run():
    if not SERVER_ID or not EMAIL or not PASSWORD:
        print("错误: 缺少必要环境变量 (ICEHOST_SERVER_ID / ICEHOST_EMAIL / ICEHOST_PASSWORD)")
        return

    from cloakbrowser import launch

    # 代理参数
    proxy_arg = PROXY if PROXY else None
    if proxy_arg:
        print(f"使用代理: {proxy_arg}")
    else:
        print("未配置 PROXY，使用直连。")

    recording_proc = None

    # CloakBrowser 以 headless=False + xvfb 方式运行
    # headless=False 时 CF 通过率更高（headless 模式部分站点仍能检测）
    browser = launch(
        headless=False,
        proxy=proxy_arg,
        geoip=True,             # 自动根据代理出口 IP 匹配时区/locale，降低 CF 风控分
        humanize=True,          # 人类化鼠标/键盘行为
    )

    try:
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="pl-PL",     # 波兰语 locale，与 IceHost 站点语言一致，降低异常风险
        )
        page = context.new_page()

        if ENABLE_RECORDING:
            recording_proc = start_recording()

        # 1. Cookie 优先登录
        logged_in = try_cookie_login(page, context)

        if not logged_in:
            # 邮箱密码登录（含 CF 验证）
            logged_in = login_with_email_password(page, context)
            if not logged_in:
                return

        # 2. 进入服务器页面（Cookie 登录时已在此页，密码登录后需跳转）
        if SERVER_URL not in page.url:
            print(f"访问服务器页面: {SERVER_URL}")
            try:
                page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"服务器页面加载异常: {e}")
            time.sleep(5)

        screenshot(page)
        page_source = page.content()

        # 3. 判定波兰语红框限制
        keywords = ["Nie możesz przedłużyć", "niedawno to zrobiłeś", "kolejne 6 godziny"]
        if any(kw in page_source for kw in keywords):
            print("检测到红框限制提示：未到续期时间，静默退出。")
            return

        # 4. 点击续期按钮（含 'dodaj 6' 文字的叶子节点，大小写不限）
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

    finally:
        try:
            browser.close()
        except Exception:
            pass
        if recording_proc:
            stop_recording(recording_proc)


if __name__ == "__main__":
    run()
