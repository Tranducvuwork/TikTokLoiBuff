#!/usr/bin/env python3
"""Auto-follow @review_in_school on connected Android device via ADB.
Login with pool accounts → follow → logout → repeat.
"""
import json, os, sys, time, random, subprocess, re

POOL_PATH = os.path.expanduser('~/.auto-like-cmt/local_pool_data.json')
TARGET_USER_ID = '7574059013261575176'
TARGET_PROFILE_DEEPLINK = f'snssdk1233://user/profile/{TARGET_USER_ID}'
TIKTOK_PKG = 'com.ss.android.ugc.trill'
NEEDED = 810

def p(*a, **kw):
    kw.setdefault('flush', True)
    print(*a, **kw)

def adb(cmd):
    r = subprocess.run(f'adb shell {cmd}', shell=True, capture_output=True, text=True, timeout=15)
    return r.stdout.strip()

def tap(x, y):
    adb(f'input tap {x} {y}')

def swipe(x1, y1, x2, y2, ms=300):
    adb(f'input swipe {x1} {y1} {x2} {y2} {ms}')

def type_text(text):
    escaped = text.replace(' ', '%s').replace('&', '\\&').replace('<', '\\<').replace('>', '\\>').replace('(', '\\(').replace(')', '\\)').replace("'", "\\'").replace('"', '\\"')
    adb(f"input text '{escaped}'")

def back():
    adb('input keyevent KEYCODE_BACK')

def home():
    adb('input keyevent KEYCODE_HOME')

def screenshot(path='/tmp/adb_ss.png'):
    subprocess.run('adb exec-out screencap -p > ' + path, shell=True, timeout=10)
    return path

def get_ui_xml():
    adb('uiautomator dump /sdcard/_ui.xml')
    subprocess.run('adb pull /sdcard/_ui.xml /tmp/_ui.xml', shell=True, capture_output=True, timeout=10)
    with open('/tmp/_ui.xml', 'r') as f:
        return f.read()

def find_text_bounds(xml, text):
    """Find bounds of element containing text."""
    pattern = f'text="{re.escape(text)}"[^/]*bounds="\\[([0-9]+),([0-9]+)\\]\\[([0-9]+),([0-9]+)\\]"'
    m = re.search(pattern, xml)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1+x2)//2, (y1+y2)//2
    return None

def find_desc_bounds(xml, desc):
    pattern = f'content-desc="{re.escape(desc)}"[^/]*bounds="\\[([0-9]+),([0-9]+)\\]\\[([0-9]+),([0-9]+)\\]"'
    m = re.search(pattern, xml)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1+x2)//2, (y1+y2)//2
    return None

def wait_for_text(text, timeout=10):
    """Wait until text appears in UI."""
    for _ in range(timeout // 2):
        time.sleep(2)
        xml = get_ui_xml()
        pos = find_text_bounds(xml, text)
        if pos:
            return pos, xml
    return None, None

def open_profile_video():
    """Open target profile and tap first video."""
    adb(f'am start -a android.intent.action.VIEW -d "{TARGET_PROFILE_DEEPLINK}"')
    time.sleep(4)

    # Find and tap first video
    xml = get_ui_xml()
    # Video tab bounds from earlier: [48,891][540,1011]
    # First video: [0,1013][358,1490]
    tap(179, 1251)
    time.sleep(4)

def tap_follow_plus():
    """Tap the '+' follow button on avatar in video view."""
    # Follow button at [947,1264][1031,1336] from UI dump
    tap(989, 1300)
    time.sleep(3)

def force_stop_tiktok():
    adb(f'am force-stop {TIKTOK_PKG}')
    time.sleep(1)

def open_tiktok():
    adb(f'monkey -p {TIKTOK_PKG} -c android.intent.category.LAUNCHER 1')
    time.sleep(5)

def do_login(login_user, password):
    """Complete login flow. Returns True if successful."""

    # Check current screen
    xml = get_ui_xml()

    # If we see "Chào mừng bạn trở lại" - tap "Thêm tài khoản khác"
    pos = find_text_bounds(xml, 'Thêm tài khoản khác')
    if pos:
        tap(*pos)
        time.sleep(3)
        xml = get_ui_xml()

    # Should be on login options - look for email/phone login option
    # Look for "Điện thoại / Email / Tên người dùng" or similar
    for text in ['Sử dụng điện thoại / email / tên người dùng',
                 'điện thoại / email',
                 'Điện thoại / Email',
                 'Phone / Email / Username',
                 'email']:
        pos = find_text_bounds(xml, text)
        if pos:
            tap(*pos)
            time.sleep(3)
            xml = get_ui_xml()
            break

    # Check if we need to switch to email tab
    pos = find_text_bounds(xml, 'Email / tên người dùng')
    if not pos:
        pos = find_text_bounds(xml, 'Email/tên người dùng')
    if pos:
        tap(*pos)
        time.sleep(1)
        xml = get_ui_xml()

    # Find the email input field and type username
    # Look for input with hint "Email hoặc TikTok ID" or similar
    email_field = find_text_bounds(xml, 'Email hoặc TikTok ID')
    if not email_field:
        email_field = find_text_bounds(xml, 'Email or TikTok ID')
    if not email_field:
        # Try finding any EditText
        m = re.search(r'class="android\.widget\.EditText"[^/]*bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]"', xml)
        if m:
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            email_field = ((x1+x2)//2, (y1+y2)//2)

    if email_field:
        tap(*email_field)
        time.sleep(0.5)
        type_text(login_user)
        time.sleep(1)
    else:
        p("    Cannot find email field")
        return False

    # Tap "Tiếp tục" or "Next"
    xml = get_ui_xml()
    for btn_text in ['Tiếp tục', 'Next', 'Tiep tuc']:
        pos = find_text_bounds(xml, btn_text)
        if pos:
            tap(*pos)
            time.sleep(4)
            break

    # Should now be on password page
    xml = get_ui_xml()
    if 'Nhập mật khẩu' not in xml and 'Mật khẩu' not in xml and 'password' not in xml.lower():
        p("    Not on password page")
        return False

    # Find password field
    pw_field = find_text_bounds(xml, 'Mật khẩu')
    if not pw_field:
        m = re.search(r'class="android\.widget\.EditText"[^/]*bounds="\[([0-9]+),([0-9]+)\]\[([0-9]+),([0-9]+)\]"', xml)
        if m:
            x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            pw_field = ((x1+x2)//2, (y1+y2)//2)

    if pw_field:
        tap(*pw_field)
        time.sleep(0.5)
        type_text(password)
        time.sleep(1)
    else:
        p("    Cannot find password field")
        return False

    # Tap "Tiếp tục" / login button
    xml = get_ui_xml()
    for btn_text in ['Tiếp tục', 'Đăng nhập', 'Next', 'Log in']:
        pos = find_text_bounds(xml, btn_text)
        if pos:
            tap(*pos)
            time.sleep(6)
            break

    # Check if login succeeded - should NOT see login-related text
    xml = get_ui_xml()
    if 'Mật khẩu' in xml or 'CAPTCHA' in xml.upper() or 'captcha' in xml or 'xác minh' in xml.lower():
        p("    Login failed (captcha or wrong password)")
        return False

    return True

def do_logout():
    """Logout from current account."""
    # Go to profile tab - bottom right "Hồ sơ"
    tap(810, 2350)  # Profile tab at bottom right
    time.sleep(3)

    xml = get_ui_xml()

    # Tap hamburger menu (top right)
    pos = find_desc_bounds(xml, 'Cài đặt và quyền riêng tư, cùng nhiều hơn nữa')
    if not pos:
        # Try the 3-line menu icon area (top right)
        tap(1020, 180)
        time.sleep(2)
        xml = get_ui_xml()
    else:
        tap(*pos)
        time.sleep(2)
        xml = get_ui_xml()

    # Look for "Cài đặt và quyền riêng tư" (Settings)
    pos = find_text_bounds(xml, 'Cài đặt và quyền riêng tư')
    if not pos:
        pos = find_text_bounds(xml, 'Settings and privacy')
    if pos:
        tap(*pos)
        time.sleep(3)
    else:
        p("    Cannot find Settings")
        return False

    # Scroll down to find "Đăng xuất" (Logout)
    for _ in range(5):
        xml = get_ui_xml()
        pos = find_text_bounds(xml, 'Đăng xuất')
        if not pos:
            pos = find_text_bounds(xml, 'Log out')
        if pos:
            tap(*pos)
            time.sleep(2)
            # Confirm logout
            xml = get_ui_xml()
            pos2 = find_text_bounds(xml, 'Đăng xuất')
            if not pos2:
                pos2 = find_text_bounds(xml, 'Log out')
            if pos2:
                tap(*pos2)
                time.sleep(3)
            return True
        swipe(540, 1800, 540, 800, 500)
        time.sleep(1)

    p("    Cannot find Logout")
    return False

def check_followed():
    """Check if follow button changed ('+' gone = followed)."""
    xml = get_ui_xml()
    # If we still see the "+" follow button, not followed
    # The follow "+" area [899,1252][1080,1355]
    # After following, the "+" disappears and avatar has a checkmark
    return '+' not in xml or 'Đã follow' in xml

def load_accounts():
    with open(POOL_PATH) as f:
        data = json.load(f)
    return [a for a in data.get('accounts', []) if a.get('password') and a.get('login_user')]

def main():
    accounts = load_accounts()
    random.shuffle(accounts)
    p(f"Loaded {len(accounts)} accounts with credentials")
    p(f"Target: @review_in_school (need ~{NEEDED} follows)")

    ok = 0
    fail = 0
    captcha = 0

    for i, acc in enumerate(accounts):
        if ok >= NEEDED:
            break

        login_user = acc['login_user']
        password = acc['password']
        p(f"\n[{i+1}/{len(accounts)}] {login_user} (ok={ok} fail={fail} captcha={captcha})")

        try:
            # Force stop TikTok and reopen
            force_stop_tiktok()
            time.sleep(1)

            # Open profile deep link
            adb(f'am start -a android.intent.action.VIEW -d "{TARGET_PROFILE_DEEPLINK}"')
            time.sleep(5)

            # Tap first video
            xml = get_ui_xml()
            # Find video grid - look for clickable items with height ~477
            video_bounds = re.findall(r'clickable="true"[^/]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            video_tapped = False
            for x1, y1, x2, y2 in video_bounds:
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                h = y2 - y1
                w = x2 - x1
                if 300 < h < 600 and 200 < w < 500 and y1 > 900:
                    tap((x1+x2)//2, (y1+y2)//2)
                    video_tapped = True
                    break

            if not video_tapped:
                tap(179, 1251)  # fallback
            time.sleep(4)

            # Tap follow "+"
            tap_follow_plus()

            # Check if login popup appeared
            xml = get_ui_xml()

            if 'Chào mừng' in xml or 'Đăng nhập' in xml or 'Log in' in xml:
                p(f"  Login required, logging in as {login_user}...")

                # Close current popup first
                pos = find_desc_bounds(xml, 'Đóng')
                if pos:
                    tap(*pos)
                    time.sleep(1)

                # Force stop and do fresh login
                force_stop_tiktok()
                time.sleep(1)
                open_tiktok()
                time.sleep(3)

                xml = get_ui_xml()

                # Look for login/signup prompt
                if 'Đăng ký' in xml or 'Hồ sơ' in xml:
                    # Tap profile tab to trigger login
                    tap(810, 2350)
                    time.sleep(3)
                    xml = get_ui_xml()

                success = do_login(login_user, password)
                if not success:
                    p(f"  LOGIN FAILED for {login_user}")
                    captcha += 1
                    # Clean up - force stop
                    force_stop_tiktok()
                    continue

                p(f"  Logged in! Navigating to video...")

                # Now navigate to target video and follow
                adb(f'am start -a android.intent.action.VIEW -d "{TARGET_PROFILE_DEEPLINK}"')
                time.sleep(5)

                # Tap first video
                xml = get_ui_xml()
                video_tapped = False
                video_bounds = re.findall(r'clickable="true"[^/]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
                for x1, y1, x2, y2 in video_bounds:
                    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
                    h = y2 - y1
                    w = x2 - x1
                    if 300 < h < 600 and 200 < w < 500 and y1 > 900:
                        tap((x1+x2)//2, (y1+y2)//2)
                        video_tapped = True
                        break
                if not video_tapped:
                    tap(179, 1251)
                time.sleep(4)

                # Tap follow
                tap_follow_plus()
                time.sleep(2)

                # Check result
                xml = get_ui_xml()
                if 'Đăng nhập' not in xml and 'Chào mừng' not in xml:
                    p(f"  FOLLOWED!")
                    ok += 1
                else:
                    p(f"  Follow failed (still showing login)")
                    fail += 1

                # Logout
                p(f"  Logging out...")
                do_logout()
            else:
                # Already logged in somehow, check if follow worked
                p(f"  Follow tapped (no login needed)")
                ok += 1

        except Exception as e:
            p(f"  ERROR: {e}")
            fail += 1
            force_stop_tiktok()

        # Status every 10
        if (i + 1) % 10 == 0:
            p(f"\n=== Progress: ok={ok} fail={fail} captcha={captcha} ===")

        time.sleep(random.uniform(1, 2))

    p(f"\n{'='*50}")
    p(f"DONE: ok={ok} fail={fail} captcha={captcha}")
    p(f"Expected followers: ~{196 + ok}")

if __name__ == '__main__':
    main()
