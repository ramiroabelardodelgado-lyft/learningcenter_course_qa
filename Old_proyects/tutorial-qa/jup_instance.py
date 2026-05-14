#!/usr/bin/env python3
"""
jup_instance.py
===============
Start or stop your LyftLearn JupyterLab instance via ml.lyft.net.
Connects to your already-running Chrome via CDP — no separate login needed.

SETUP (one time):
    Close Chrome, then reopen it with remote debugging enabled:
        /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 &

    Or add this alias to your ~/.zshrc for convenience:
        alias chrome='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222'

Usage:
    python3 jup_instance.py --start
    python3 jup_instance.py --stop
    python3 jup_instance.py --status

Install:
    pip install playwright --index-url https://pypi.org/simple/
    playwright install chromium
"""

import sys
import time
import argparse

ML_LYFT_URL = "https://ml.lyft.net/jupyterlab-instances"
CDP_URL = "http://localhost:9222"


def get_page(playwright):
    """Connect to the already-running Chrome via CDP."""
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
    except Exception:
        print("❌ Could not connect to Chrome on port 9222.")
        print()
        print("   Chrome needs to be running with remote debugging enabled.")
        print("   Close Chrome completely, then run:")
        print()
        print('   /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222 &')
        print()
        print("   Or add this alias to ~/.zshrc:")
        print("   alias chrome='/Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222'")
        sys.exit(1)

    # Use existing context (your logged-in session)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    page = context.new_page()
    page.goto(ML_LYFT_URL, wait_until="networkidle", timeout=30_000)
    return browser, page


def get_status(page):
    if page.locator("text=Running").count() > 0:
        return "running"
    if page.locator("text=Starting").count() > 0:
        return "starting"
    if page.locator("text=Stopping").count() > 0:
        return "stopping"
    if page.locator("button:has-text('Start'), a:has-text('Start')").count() > 0:
        return "stopped"
    return "unknown"


def cmd_status():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page = get_page(p)
        status = get_status(page)
        icons = {
            "running":  "✅ Running",
            "starting": "⏳ Starting...",
            "stopping": "🛑 Stopping...",
            "stopped":  "⭕ Stopped",
            "unknown":  "❓ Unknown — screenshot saved to ml_lyft_debug.png",
        }
        print(f"\n  Instance status: {icons.get(status, status)}\n")
        if status == "unknown":
            page.screenshot(path="ml_lyft_debug.png")
        page.close()
        browser.close()
        return status


def cmd_start():
    from playwright.sync_api import sync_playwright
    print("🚀 Starting JupyterLab instance...")

    with sync_playwright() as p:
        browser, page = get_page(p)
        status = get_status(page)

        if status == "running":
            print("  ✅ Already running — nothing to do")
            page.close()
            browser.close()
            return True

        if status == "starting":
            print("  ⏳ Already starting — waiting for it to be ready...")
        elif status == "stopped":
            start_btn = page.locator("button:has-text('Start'), a:has-text('Start')")
            print("  🖱️  Clicking Start...")
            start_btn.first.click()
        else:
            print(f"  ⚠️  Unexpected status: {status}")
            page.screenshot(path="ml_lyft_debug.png")
            print("     Screenshot saved to ml_lyft_debug.png")
            page.close()
            browser.close()
            return False

        # Poll until Running
        print("  ⏳ Waiting for instance to be Running...")
        for attempt in range(24):  # max ~4 minutes
            time.sleep(10)
            page.reload(wait_until="networkidle")
            status = get_status(page)
            elapsed = (attempt + 1) * 10
            print(f"     [{elapsed:3d}s] {status}")

            if status == "running":
                print(f"\n  ✅ Instance is Running! ({elapsed}s)\n")
                page.close()
                browser.close()
                return True

        print("\n  ❌ Timed out waiting for instance to start")
        page.screenshot(path="ml_lyft_timeout.png")
        page.close()
        browser.close()
        return False


def cmd_stop():
    from playwright.sync_api import sync_playwright
    print("🛑 Stopping JupyterLab instance...")

    with sync_playwright() as p:
        browser, page = get_page(p)
        status = get_status(page)

        if status in ("stopped", "stopping"):
            print(f"  ℹ️  Already {status} — nothing to do")
            page.close()
            browser.close()
            return True

        stop_btn = page.locator("button:has-text('Stop'), a:has-text('Stop')")
        if stop_btn.count() == 0:
            print("  ⚠️  No Stop button found")
            page.screenshot(path="ml_lyft_debug.png")
            page.close()
            browser.close()
            return False

        print("  🖱️  Clicking Stop...")
        stop_btn.first.click()
        time.sleep(3)
        print("  ✅ Stop signal sent — instance is shutting down\n")
        page.close()
        browser.close()
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Start or stop your LyftLearn JupyterLab instance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Requires Chrome running with remote debugging:
    /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222 &

Examples:
    python3 jup_instance.py --start
    python3 jup_instance.py --stop
    python3 jup_instance.py --status
        """,
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--start",  action="store_true", help="Start the instance")
    group.add_argument("--stop",   action="store_true", help="Stop the instance")
    group.add_argument("--status", action="store_true", help="Check current status")

    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright  # noqa
    except ImportError:
        print("❌ Playwright not installed. Run:")
        print("   pip install playwright --index-url https://pypi.org/simple/")
        print("   playwright install chromium")
        sys.exit(1)

    if args.start:
        ok = cmd_start()
        sys.exit(0 if ok else 1)
    elif args.stop:
        ok = cmd_stop()
        sys.exit(0 if ok else 1)
    elif args.status:
        cmd_status()
        sys.exit(0)


if __name__ == "__main__":
    main()