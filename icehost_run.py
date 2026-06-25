import os
import time
import json
import signal
import subprocess
import urllib.parse
import requests
from seleniumbase import SB

BASE_URL = os.getenv("ICEHOST_BASE_URL", "https://dash.icehost.pl")
SERVER_ID = os.getenv("ICEHOST_SERVER_ID", "").strip()
SERVER_URL = f"{BASE_URL}/server/{SERVER_ID}" if SERVER_ID else None
EMAIL = os.getenv("ICEHOST_EMAIL")
PASSWORD = os.getenv("ICEHOST_PASSWORD")
LOGIN_URL = os.getenv("ICEHOST_LOGIN_URL", f"{BASE_URL}/auth/login")

# 持久化保存 Cookie 的文件路径（由 workflow 通过 GitHub Actions Cache 在多次运行间保留）
COOKIE_FILE = os.getenv("ICEHOST_COOKIE_FILE", "state/icehost_cookies.json")

# SOCKS5 代理地址，和 freecp 项目同款方案：workflow 里用 Xray 起一个本地 SOCKS5 端口，
# 这里直接把地址传给浏览器。不需要代理的话可以把 PROXY 环境变量设为空字符串。
PROXY = os.getenv("PROXY", "socks5://127.0.0.1:10808").strip()

# true/false 字符串，来自 workflow_dispatch 的下拉框输入
ENABLE_RECORDING = os.getenv("ENABLE_RECORDING", "false").strip().lower() == "true"
RECORD_FILE = "icehost_record.mp4"
SCREENSHOT_FILE = "icehost_debug_screenshot.png"

# 录屏兜底分辨率（拿不到真实 Xvfb 分辨率时使用）
XVFB_WIDTH = 1366
XVFB_HEIGHT = 768


def send_tg_notification(message, photo_path=None):
    """发送结果文字和截图至 Telegram"""
    token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")
    if not token or not chat_id:
        print("未配置 TG 机器人变量，跳过发送 TG 推送。")
        return

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload)
        print("TG 状态通知发送成功。")
    except Exception as e:
        print(f"发送 TG 消息异常: {e}")

    if photo_path and os.path.exists(photo_path):
        try:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                data = {"chat_id": chat_id, "caption": "IceHost 实时画面"}
                requests.post(url, data=data, files=files)
            print("TG 截图发送成功。")
        except Exception as e:
            print(f"发送 TG 截图异常: {e}")


def get_display_resolution(display):
    """用 xdpyinfo 探测虚拟显示器的真实分辨率，探测失败则回退到默认值"""
    try:
        out = subprocess.check_output(
            ["xdpyinfo", "-display", display], stderr=subprocess.DEVNULL
        ).decode()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("dimensions:"):
                # 形如: dimensions:    1920x1080 pixels (508x285 millimeters)
                dims = line.split()[1]
                w, h = dims.split("x")
                return int(w), int(h)
    except Exception as e:
        print(f"探测显示器分辨率失败，使用默认 {XVFB_WIDTH}x{XVFB_HEIGHT}: {e}")
    return XVFB_WIDTH, XVFB_HEIGHT


def start_recording():
    """在虚拟显示器上启动 ffmpeg 屏幕录制，返回 Popen 对象（失败则返回 None）"""
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
        time.sleep(1)  # 给 ffmpeg 一点启动时间
        return proc
    except Exception as e:
        print(f"启动 ffmpeg 录屏失败，本次跳过录屏: {e}")
        return None


def stop_recording(proc):
    """优雅停止 ffmpeg，确保 mp4 文件正确收尾（写入 moov atom）"""
    if not proc:
        return
    try:
        proc.send_signal(signal.SIGINT)  # 让 ffmpeg 正常 finalize 文件
        proc.wait(timeout=15)
        print("录屏已正常停止并保存。")
    except Exception as e:
        print(f"停止录屏异常，强制 kill: {e}")
        try:
            proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Cookie 持久化：保存 / 读取 / 注入
# ---------------------------------------------------------------------------

def load_saved_cookies():
    """读取上次保存的 Cookie，没有或读取失败返回 None"""
    if not os.path.exists(COOKIE_FILE):
        print(f"未找到本地 Cookie 文件（{COOKIE_FILE}），本次跳过 Cookie 登录尝试。")
        return None
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
        return None
    except Exception as e:
        print(f"读取本地 Cookie 文件失败，忽略: {e}")
        return None


def save_cookies(sb):
    """把当前浏览器的 Cookie 写到本地文件，供下次运行优先使用"""
    try:
        cookies = sb.get_cookies()
        os.makedirs(os.path.dirname(COOKIE_FILE) or ".", exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        print(f"已保存最新 Cookie 到 {COOKIE_FILE}，下次将优先尝试 Cookie 登录。")
    except Exception as e:
        print(f"保存 Cookie 失败（不影响本次续期结果）: {e}")


def inject_cookies(sb, cookies):
    """把保存的 Cookie 注入到当前浏览器会话"""
    ok_count = 0
    for c in cookies:
        try:
            raw_value = c.get("value", "")
            try:
                decoded_value = urllib.parse.unquote(raw_value)
            except Exception:
                decoded_value = raw_value

            cookie_dict = {
                "name": c["name"],
                "value": decoded_value,
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
            }
            if c.get("sameSite"):
                ss = str(c["sameSite"]).lower()
                if ss in ["lax", "strict", "none"]:
                    cookie_dict["sameSite"] = ss.capitalize()

            sb.add_cookie(cookie_dict)
            ok_count += 1
        except Exception as e:
            print(f"注入 Cookie {c.get('name')} 失败，跳过: {e}")
    print(f"成功注入 {ok_count}/{len(cookies)} 个 Cookie。")


def try_cookie_login(sb):
    """优先尝试用已保存的 Cookie 登录，成功返回 True，失败/无 Cookie 返回 False"""
    saved = load_saved_cookies()
    if not saved:
        return False

    print("尝试使用已保存的 Cookie 登录...")
    sb.uc_open_with_reconnect(SERVER_URL, reconnect_time=6)
    sb.sleep(3)
    inject_cookies(sb, saved)
    sb.refresh()
    sb.sleep(5)
    sb.save_screenshot(SCREENSHOT_FILE)

    current_url = sb.get_current_url()
    if "auth/login" in current_url or sb.is_element_visible("input[name='username']"):
        print("Cookie 已失效（被重定向回登录页），改用邮箱密码登录流程。")
        return False

    print(f"Cookie 登录成功！当前 URL: {current_url}")
    return True


def click_turnstile_by_iframe(sb):
    """
    通过枚举 iframe 找到 Cloudflare Turnstile，用 bounding_box 精准点击。
    uc_cdp_events 模式下作为兜底手段。
    """
    try:
        driver = sb.driver
        frames = driver.find_elements("tag name", "iframe")
        print(f"当前页面共找到 {len(frames)} 个 iframe")
        for i, frame in enumerate(frames):
            try:
                src = frame.get_attribute("src") or ""
                print(f"  iframe[{i}] src: {src[:80]}")
                if "challenges.cloudflare.com" in src or "turnstile" in src:
                    print(f"  → 找到 Turnstile iframe[{i}]，计算坐标...")
                    rect = driver.execute_script(
                        "const r = arguments[0].getBoundingClientRect();"
                        "return {x: r.x, y: r.y, w: r.width, h: r.height};",
                        frame
                    )
                    click_x = rect["x"] + rect["w"] * 0.12
                    click_y = rect["y"] + rect["h"] * 0.5
                    print(f"  → 点击坐标: ({click_x:.1f}, {click_y:.1f})")
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(driver).move_by_offset(click_x, click_y).click().perform()
                    ActionChains(driver).move_by_offset(-click_x, -click_y).perform()
                    return True
            except Exception as fe:
                print(f"  iframe[{i}] 处理异常: {fe}")
                continue
        print("未找到 Turnstile iframe，回退到 uc_gui_click_captcha()")
        sb.uc_gui_click_captcha()
        return True
    except Exception as e:
        print(f"click_turnstile_by_iframe 异常: {e}")
        return False


def login_with_email_password(sb):
    """走完整的 CF 验证 + 邮箱密码登录流程，成功返回 True，失败返回 False"""
    for attempt in range(1, 4):
        print(f"[CF 验证] 第 {attempt}/3 次尝试，访问登录页: {LOGIN_URL}")

        # uc_cdp_events 模式：用更长的 reconnect_time 让 CF 自动验证先走完
        # reconnect_time=8 → SB 断开重连一次，给 CF JS challenge 足够时间执行
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=8)
        sb.sleep(3)
        sb.save_screenshot(SCREENSHOT_FILE)

        # 情况1：CF 自动通过，直接进了登录页
        if sb.is_element_visible("input[name='username']"):
            print("CF 自动通过（uc_cdp_events），已进入登录页。")
            break

        print("仍在 CF 验证页，尝试 uc_gui_handle_cf() ...")
        # uc_gui_handle_cf 是 uc_cdp_events 模式专用方法，
        # 内部会监听 CF challenge 完成事件并在合适时机点击
        try:
            sb.uc_gui_handle_cf()
            sb.sleep(3)
        except Exception as e:
            print(f"uc_gui_handle_cf 异常（非致命）: {e}")

        sb.save_screenshot(SCREENSHOT_FILE)

        # 情况2：handle_cf 处理后进了登录页
        if sb.is_element_visible("input[name='username']"):
            print("uc_gui_handle_cf 处理后已进入登录页。")
            break

        print("uc_gui_handle_cf 未通过，改用 iframe 枚举精准点击...")
        click_turnstile_by_iframe(sb)
        sb.sleep(2)
        sb.save_screenshot(SCREENSHOT_FILE)

        # 点击后等最多 30 秒等跳转
        passed = False
        for _ in range(30):
            sb.sleep(1)
            if sb.is_element_visible("input[name='username']"):
                passed = True
                break

        if passed:
            print(f"第 {attempt} 次 CF 验证通过，已进入登录页。")
            break
        else:
            print(f"第 {attempt}/3 次 CF 验证未通过，截图后重试...")
            sb.save_screenshot(SCREENSHOT_FILE)
            if attempt == 3:
                send_tg_notification(
                    "❌ <b>IceHost CF 验证失败（已重试 3 次），请检查截图。</b>",
                    SCREENSHOT_FILE,
                )
                return False

    sb.save_screenshot(SCREENSHOT_FILE)

    # 填写邮箱和密码并登录
    print("填写登录表单...")
    try:
        sb.wait_for_element_visible("input[name='username']", timeout=10)
        sb.type("input[name='username']", EMAIL)
        sb.type("input[name='password']", PASSWORD)
        sb.save_screenshot(SCREENSHOT_FILE)

        sb.click("button[type='submit']")
        print("已提交登录表单，等待跳转...")
        sb.sleep(5)
    except Exception as e:
        print(f"填写登录表单失败: {e}")
        sb.save_screenshot(SCREENSHOT_FILE)
        send_tg_notification(
            "❌ <b>IceHost 登录表单操作失败，请检查截图。</b>", SCREENSHOT_FILE
        )
        return False

    # 判断是否登录成功
    current_url = sb.get_current_url()
    if "auth/login" in current_url or sb.is_element_visible("input[name='username']"):
        print(f"登录失败，当前 URL: {current_url}")
        sb.save_screenshot(SCREENSHOT_FILE)
        send_tg_notification(
            "❌ <b>IceHost 登录失败！请检查邮箱/密码是否正确。</b>", SCREENSHOT_FILE
        )
        return False

    print(f"邮箱密码登录成功，当前 URL: {current_url}")
    return True


def run():
    if not SERVER_ID or not EMAIL or not PASSWORD:
        print("错误: 缺少必要环境变量 (ICEHOST_SERVER_ID / ICEHOST_EMAIL / ICEHOST_PASSWORD)")
        return

    sb_kwargs = dict(uc=True, xvfb=True, uc_cdp_events=True)

    if PROXY:
        print(f"使用代理: {PROXY}")
        sb_kwargs["proxy"] = PROXY
    else:
        print("未配置 PROXY，使用直连。")

    recording_proc = None

    with SB(**sb_kwargs) as sb:
        if ENABLE_RECORDING:
            recording_proc = start_recording()

        try:
            # 1. 优先尝试 Cookie 登录，失败则回退到邮箱密码登录
            logged_in = try_cookie_login(sb)

            if not logged_in:
                logged_in = login_with_email_password(sb)
                if not logged_in:
                    return  # 失败通知已在内部发送
                # 邮箱密码登录成功后，提取最新 Cookie 存起来，供下次优先使用
                save_cookies(sb)

            # 2. 进入服务器页面
            print(f"访问服务器页面: {SERVER_URL}")
            sb.open(SERVER_URL)
            sb.sleep(5)
            sb.save_screenshot(SCREENSHOT_FILE)

            # 3. 判定波兰语红框限制（未到续期时间）
            page_source = sb.get_page_source()
            keywords = ["Nie możesz przedłużyć", "niedawno to zrobiłeś", "kolejne 6 godziny"]
            is_limited = any(kw in page_source for kw in keywords)

            if is_limited:
                print("检测到红框限制提示：未到续期时间，静默退出（不发送 TG 提醒）。")
                return

            # 4. 查找并点击续期按钮
            renew_btn_selector = (
                "//*[not(*) and contains(translate(., "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'dodaj 6')]"
            )

            try:
                print("等待续期按钮加载...")
                sb.wait_for_element_visible(renew_btn_selector, timeout=15)
                print("找到续期按钮，点击...")
                sb.click(renew_btn_selector)

                # 点击后不刷新，先等 5 秒读 DOM，区分"真续期成功"和"点了但时间未到"
                sb.sleep(5)
                sb.save_screenshot(SCREENSHOT_FILE)

                current_source = sb.get_page_source()
                if any(kw in current_source for kw in keywords):
                    print("点击后立刻弹出限制提示：未到可续期时间（续期未成功），静默退出。")
                    return

                print("点击后未检测到限制提示，刷新页面确认最终结果...")
                sb.refresh()
                sb.sleep(5)
                sb.save_screenshot(SCREENSHOT_FILE)

                updated_source = sb.get_page_source()
                if any(kw in updated_source for kw in keywords):
                    msg = "⚡ <b>IceHost 续期成功！</b>\n服务器已成功延长 6 小时有效期。"
                else:
                    msg = "ℹ️ <b>IceHost 续期指令已发送</b>\n请查看截图确认结果。"
                print(msg)
                send_tg_notification(msg, SCREENSHOT_FILE)

            except Exception as e:
                print(f"未找到续期按钮（可能已续满，或按钮标签发生变动）: {e}")

        finally:
            # 无论成功/失败/异常，都要确保 ffmpeg 被正常收尾，否则 mp4 文件会损坏打不开
            if recording_proc:
                stop_recording(recording_proc)


if __name__ == "__main__":
    run()
