#!/usr/bin/env python3
"""
run_qa_pipeline.py
==================
Laptop-side orchestrator for the Language QA Pipeline.

Uses Playwright to start the LyftLearn instance via ml.lyft.net (reusing
your existing browser session), then pipes commands through llt ssh connect.

Usage:
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --name "De-escalation"
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --name "De-escalation" --dry-run
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --name "De-escalation" --no-lifecycle

Install:
    pip install playwright python-dotenv
    playwright install chromium
"""

import os
import sys
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ═══════════════════════════════════════════════════════════════════════
# ⚙️  CONFIG
# ═══════════════════════════════════════════════════════════════════════

# ml.lyft.net instance management URL
ML_LYFT_URL = "https://ml.lyft.net/jupyterlab-instances"

# Your Chrome profile path (already logged into Lyft SSO)
CHROME_PROFILE = os.path.expanduser(
    "~/Library/Application Support/Google/Chrome"
)

# Where scripts live on the instance
REMOTE_SCRIPTS_DIR = "~/studio"


# ═══════════════════════════════════════════════════════════════════════
# Instance Lifecycle via Playwright
# ═══════════════════════════════════════════════════════════════════════

def start_instance_playwright(headless=False):
    """
    Open ml.lyft.net and click Start on the instance.
    Reuses your existing Chrome profile so no re-auth needed.
    Returns True when instance is Running.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright not installed.")
        print("   pip install playwright && playwright install chromium")
        sys.exit(1)

    print("🌐 Opening ml.lyft.net to start instance...")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=headless,
            args=["--no-first-run", "--no-default-browser-check"],
        )

        page = browser.new_page()
        page.goto(ML_LYFT_URL, wait_until="networkidle", timeout=30_000)
        print(f"  📄 Loaded: {page.title()}")

        # Already running?
        if page.locator("text=Running").count() > 0:
            print("  ✅ Instance already Running")
            browser.close()
            return True

        # Click Start
        start_btn = page.locator("button:has-text('Start'), a:has-text('Start')")
        if start_btn.count() == 0:
            print("  ⚠️  No Start button found — saving screenshot for debugging")
            page.screenshot(path="ml_lyft_debug.png")
            print("     Saved: ml_lyft_debug.png — check what's on the page")
            browser.close()
            return False

        print("  🚀 Clicking Start...")
        start_btn.first.click()

        # Poll until Running
        print("  ⏳ Waiting for instance to be Running...")
        for attempt in range(24):  # max ~4 minutes
            time.sleep(10)
            page.reload(wait_until="networkidle")
            elapsed = (attempt + 1) * 10

            if page.locator("text=Running").count() > 0:
                print(f"  ✅ Instance is Running! ({elapsed}s)")
                browser.close()
                return True

            status = "Starting..." if page.locator("text=Starting").count() > 0 else "waiting"
            print(f"     [{elapsed:3d}s] {status}")

        print("  ❌ Timed out waiting for instance to start")
        page.screenshot(path="ml_lyft_timeout.png")
        browser.close()
        return False


def stop_instance_playwright(headless=False):
    """Click Stop on the instance at ml.lyft.net."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return

    print("\n🛑 Stopping instance via ml.lyft.net...")

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=CHROME_PROFILE,
            headless=headless,
            args=["--no-first-run"],
        )
        page = browser.new_page()
        page.goto(ML_LYFT_URL, wait_until="networkidle", timeout=30_000)

        stop_btn = page.locator("button:has-text('Stop'), a:has-text('Stop')")
        if stop_btn.count() > 0:
            stop_btn.first.click()
            time.sleep(3)
            print("  ✅ Stop signal sent — instance shutting down")
        else:
            print("  ℹ️  No Stop button found — may already be stopped")

        browser.close()


# ═══════════════════════════════════════════════════════════════════════
# SSH Command Execution via llt ssh connect stdin piping
# ═══════════════════════════════════════════════════════════════════════

def ssh_run_commands(commands: list) -> bool:
    """
    Run commands on the instance by piping a shell script
    into llt ssh connect via stdin.

    llt opens a standard SSH shell, so stdin piping works
    even though it doesn't accept commands as arguments.
    """
    # set -e: exit immediately if any command fails
    script = "set -e\nsource ~/.bashrc\n" + "\n".join(commands) + "\nexit 0\n"

    print(f"\n  📡 Running on instance:")
    for cmd in commands:
        if cmd and not cmd.startswith("echo"):
            print(f"     $ {cmd}")

    result = subprocess.run(
        ["llt", "ssh", "connect"],
        input=script,
        text=True,
        # Stream output live — don't capture
        stdout=None,
        stderr=None,
    )

    return result.returncode == 0


# ═══════════════════════════════════════════════════════════════════════
# Pipeline Steps
# ═══════════════════════════════════════════════════════════════════════

def run_extract(course_id, course_name=None, languages=None) -> bool:
    print("\n" + "═" * 55)
    print("STEP 2/4  Extract Course from Contentful")
    print("═" * 55)

    cmd = f"cd {REMOTE_SCRIPTS_DIR} && python extract_course.py --course {course_id}"
    if course_name:
        cmd += f' --name "{course_name}"'
    if languages:
        cmd += f" --languages {languages}"

    return ssh_run_commands([cmd])


def run_qa(course_name, skip_en=True, dry_run=False, locale=None) -> bool:
    print("\n" + "═" * 55)
    print(f"STEP 3/4  Language QA {'(dry run — no LLM)' if dry_run else '(Claude)'}")
    print("═" * 55)

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in (course_name or "")
    ).strip()
    input_dir = f"$HOME/studio/output/{safe_name}"

    cmd = f'cd $HOME/studio && python language_qa.py --input "{input_dir}" --csv --save'
    if skip_en:
        cmd += " --skip-en"
    if dry_run:
        cmd += " --dry-run"
    if locale:
        cmd += f" --locale {locale}"

    return ssh_run_commands([cmd])


def print_download_instructions(course_name):
    """llt doesn't have scp — print where the files are."""
    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in (course_name or "")
    ).strip()

    print("\n" + "═" * 55)
    print("STEP 4/4  Download Reports")
    print("═" * 55)
    print(f"""
  Reports saved on instance at:
    ~/studio/output/{safe_name}/

  To download:
    Option A — JupyterLab file browser
      Open go/ml → click JupyterLab → navigate to
      studio/output/{safe_name}/ → right-click CSV → Download

    Option B — SSH + cat (pipe to laptop)
      llt ssh connect
      cat ~/studio/output/{safe_name}/*_issues.csv
""")


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Run Language QA pipeline: start → extract → QA → stop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --name "De-escalation"
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --dry-run
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --no-lifecycle
    python run_qa_pipeline.py --course 2yQq04tUUk1H67xlZA7PLn --keep-running
        """,
    )

    parser.add_argument("--course", "-c", required=True, help="Contentful course/container entry ID")
    parser.add_argument("--name", "-n", default=None, help="Course name (used for output folder)")
    parser.add_argument("--languages", default=None, help="Comma-separated locales (default: all)")
    parser.add_argument("--locale", "-l", default=None, help="QA only this locale")
    parser.add_argument("--skip-en", action="store_true", default=True, help="Skip English QA (default: on)")
    parser.add_argument("--no-skip-en", dest="skip_en", action="store_false", help="Include English in QA")
    parser.add_argument("--dry-run", action="store_true", help="Prescan only — no LLM calls")
    parser.add_argument("--no-lifecycle", action="store_true",
                        help="Skip start/stop — use when instance is already running")
    parser.add_argument("--keep-running", action="store_true",
                        help="Don't stop the instance after pipeline completes")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser invisibly (no window)")

    args = parser.parse_args()
    course_name = args.name or args.course

    print(f"""
╔══════════════════════════════════════════════════════╗
║         Language QA Pipeline — Orchestrator          ║
╚══════════════════════════════════════════════════════╝

  Course:    {args.course}
  Name:      {course_name}
  Skip EN:   {args.skip_en}
  Dry run:   {args.dry_run}
  Lifecycle: {'disabled' if args.no_lifecycle else 'Playwright → ml.lyft.net'}
""")

    instance_started_by_us = False

    try:
        # ── Step 1: Start ──────────────────────────────────────────────
        if not args.no_lifecycle:
            print("═" * 55)
            print("STEP 1/4  Start Instance")
            print("═" * 55)
            ok = start_instance_playwright(headless=args.headless)
            if not ok:
                print("❌ Could not start instance. Aborting.")
                sys.exit(1)
            instance_started_by_us = True
            print("  ⏳ Waiting 15s for SSH to be ready...")
            time.sleep(15)
        else:
            print("⏭️  Skipping instance lifecycle (--no-lifecycle)")

        # ── Step 2: Extract ────────────────────────────────────────────
        ok = run_extract(args.course, args.name, args.languages)
        if not ok:
            print("❌ Extraction failed. Check .env on instance has CONTENTFUL_* vars set.")
            sys.exit(1)

        # ── Step 3: QA ─────────────────────────────────────────────────
        ok = run_qa(course_name, skip_en=args.skip_en, dry_run=args.dry_run, locale=args.locale)
        if not ok:
            print("⚠️  QA step had errors — check output above for details")

        # ── Step 4: Download instructions ─────────────────────────────
        print_download_instructions(course_name)

    finally:
        if instance_started_by_us and not args.no_lifecycle and not args.keep_running:
            stop_instance_playwright(headless=args.headless)

    print("✅ Pipeline complete!\n")


if __name__ == "__main__":
    main()