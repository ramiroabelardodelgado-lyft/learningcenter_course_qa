#!/usr/bin/env python3
"""
screenshot_runner.py
====================
Headless Playwright screenshot engine for LyftLearn tutorial pages.

Merges Tampermonkey JS fixes (content resize, video seek timing, 3-tier
navigation) with Playwright's native screenshot capability so this runs
headlessly on the SageMaker instance with no human at a keyboard.

Interface mirrors runner.py — called from poller.py when job_type == "screenshots"

Usage (manual test):
    cd $HOME/studio
    python screenshot_module/screenshot_runner.py --course 2yQq04tUUk1H67xlZA7PLn

Environment variables:
    STAGING_BASE_URL   https://staging.lyft.net/learningcenter
    STAGING_PHONE      +15555555555
    S3_BUCKET          lyft-lyftlearn-production-iad  (default)
"""

import asyncio
import argparse
import os
import sys
import json
import traceback
import zipfile
import uuid
from pathlib import Path
from datetime import datetime, timezone

# ── Module dependencies path setup ─────────────────────────────────────────
# Playwright installed to screenshot_module/.local-packages (persists in EFS)
_module_dir = Path(__file__).parent
_local_packages = _module_dir / ".local-packages"
_local_browsers = _module_dir / ".local-browsers"

if _local_packages.exists():
    sys.path.insert(0, str(_local_packages))

# Set Playwright browser path to our persistent EFS location
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_local_browsers))
# ──────────────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════

DEFAULT_LOCALES = ["en", "es", "fr", "pt"]

# Mobile viewport matching tutorial_screenshots.py (iPhone 17 logical pixels)
VIEWPORT = {"width": 393, "height": 852}
DEVICE_SCALE_FACTOR = 3
USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
    "Mobile/15E148 Safari/604.1"
)

# Delay after navigation before screenshot (ms)
PAGE_SETTLE_MS = 1200

# Max video wait (ms) — TM had no cap, but some videos are very long
MAX_VIDEO_WAIT_MS = 8000

# Stop if this many consecutive screenshots fail
MAX_CONSECUTIVE_FAILURES = 3


# ═══════════════════════════════════════════════════════════════════════
# Tampermonkey fixes — ported to page.evaluate()
# ═══════════════════════════════════════════════════════════════════════

# Ported from: Audit_Lesson_Navigation userscript (resize function)
RESIZE_JS = """
() => {
    try {
        const wrapper = document.querySelector('[data-testid="simple-text-wrapper"]');
        if (wrapper && wrapper.parentElement) {
            wrapper.parentElement.style.cssText =
                'height: 100%; min-height: calc(-168px + 100vh);';
        }
    } catch(e) {}
}
"""

# Ported from: reloadData() + videoTimer logic in Tampermonkey
# TM waited: videoTimer = Math.round(videos[v].duration * .777 * 100) ms
# We seek directly instead of waiting for playback — same frame, much faster.
VIDEO_PREP_JS = """
() => {
    let maxWaitMs = 0;
    try {
        const videos = document.querySelectorAll('video');
        videos.forEach(v => {
            if (v.duration && isFinite(v.duration) && v.duration > 0) {
                const targetTime = v.duration * 0.777;
                v.currentTime = targetTime;
                // Match TM's original wait calculation
                const waitMs = Math.round(v.duration * 0.777 * 100);
                maxWaitMs = Math.max(maxWaitMs, waitMs);
            }
            v.play().catch(() => {});
        });
    } catch(e) {}
    return maxWaitMs;
}
"""


async def prepare_page(page) -> int:
    """
    Apply Tampermonkey fixes to the current page before screenshotting.
    Returns video_wait_ms (0 if no video on page).
    """
    await page.evaluate(RESIZE_JS)
    video_wait_ms = await page.evaluate(VIDEO_PREP_JS)
    return min(int(video_wait_ms), MAX_VIDEO_WAIT_MS)


# ═══════════════════════════════════════════════════════════════════════
# Quiz handling — fetch correct answers from Contentful
# ═══════════════════════════════════════════════════════════════════════

def fetch_course_structure(course_id: str) -> dict:
    """
    Fetch course structure with quiz positions and answers.
    Returns dict: {
        "page_index": {page_num: {"type": "quiz", "questions": [{text, answers, correct_index}]}},
        "has_quizzes": bool
    }
    """
    import requests

    space_id = os.environ.get("CONTENTFUL_SPACE_ID", "kdr36sxfa9m3")
    cma_token = os.environ.get("CONTENTFUL_CMA_TOKEN")
    env_id = os.environ.get("CONTENTFUL_ENVIRONMENT_ID", "master")

    if not cma_token:
        print("    ⚠️  No CONTENTFUL_CMA_TOKEN — cannot fetch course structure")
        return {"page_index": {}, "has_quizzes": False}

    try:
        headers = {"Authorization": f"Bearer {cma_token}"}
        base = f"https://api.contentful.com/spaces/{space_id}/environments/{env_id}"

        # Fetch course entry
        resp = requests.get(f"{base}/entries/{course_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"    ⚠️  Failed to fetch course: {resp.status_code}")
            return {"page_index": {}, "has_quizzes": False}

        course = resp.json()
        lessons = course.get("fields", {}).get("lessons", {}).get("en-US", [])

        page_index = {}
        page_num = 1
        has_quizzes = False

        for lesson_ref in lessons:
            lesson_id = lesson_ref.get("sys", {}).get("id")
            if not lesson_id:
                continue

            # Fetch lesson
            lesson_resp = requests.get(f"{base}/entries/{lesson_id}", headers=headers, timeout=15)
            if lesson_resp.status_code != 200:
                continue

            lesson = lesson_resp.json()
            lesson_type = lesson.get("sys", {}).get("contentType", {}).get("sys", {}).get("id")

            # Handle quiz lessons
            if lesson_type == "quiz":
                has_quizzes = True
                quiz_activities = lesson.get("fields", {}).get("quizActivities", {}).get("en-US", [])

                questions = []
                for q_ref in quiz_activities:
                    q_id = q_ref.get("sys", {}).get("id")
                    if not q_id:
                        continue

                    # Fetch question
                    q_resp = requests.get(f"{base}/entries/{q_id}", headers=headers, timeout=15)
                    if q_resp.status_code != 200:
                        continue

                    question = q_resp.json()
                    fields = question.get("fields", {})

                    q_text = fields.get("displayQuestion", {}).get("en-US", "")
                    answers = [
                        fields.get("answer1", {}).get("en-US", ""),
                        fields.get("answer2", {}).get("en-US", ""),
                        fields.get("answer3", {}).get("en-US", ""),
                        fields.get("answer4", {}).get("en-US", ""),
                    ]
                    # correctAnswer is 1-indexed in Contentful
                    correct_index = fields.get("correctAnswer", {}).get("en-US", 1) - 1

                    questions.append({
                        "text": q_text,
                        "answers": [a for a in answers if a],  # Filter empty
                        "correct_index": correct_index
                    })
                    page_num += 1  # Each question is a page

                if questions:
                    # Quiz intro page comes before questions
                    page_index[page_num] = {"type": "quiz_start", "questions": questions}
                    print(f"    📝 Quiz at page ~{page_num} ({len(questions)} questions)")
            else:
                # Regular lesson - count activities
                activities = lesson.get("fields", {}).get("lessonActivities", {}).get("en-US", [])
                page_num += len(activities) if activities else 1

        if has_quizzes:
            print(f"    ✅ Found quizzes in course structure")

        return {"page_index": page_index, "has_quizzes": has_quizzes}

    except Exception as e:
        print(f"    ⚠️  Error fetching course structure: {e}")
        import traceback
        traceback.print_exc()
        return {"page_index": {}, "has_quizzes": False}


async def handle_quiz(page, page_num: int, course_structure: dict) -> bool:
    """
    Detect if current page is a quiz and click through it.
    Just clicks the first answer to get past the quiz.
    Returns True if a quiz was handled, False otherwise.
    """
    # Detect quiz by looking for quiz UI elements
    quiz_indicators = [
        'text=/knowledge check/i',
        'text=/what is the benefit of/i',
        'text=/you had a small accident/i',
        'button:has-text("Show me the answer")',
        '[role="radiogroup"]',
    ]

    is_quiz = False
    for indicator in quiz_indicators:
        if await page.locator(indicator).count() > 0:
            is_quiz = True
            break

    if not is_quiz:
        return False

    print("    📝 Quiz detected - clicking through...")

    try:
        # Strategy: Just click the first answer option and submit
        await page.wait_for_timeout(500)

        # Try to find and click the first answer option
        answer_clicked = False

        # Try different answer selectors
        answer_selectors = [
            '[role="radio"]',
            'button[role="radio"]',
            'div[role="radio"]',
            '[data-testid^="answer"]',
            'button:has-text("You")',  # Common answer pattern
        ]

        for selector in answer_selectors:
            options = page.locator(selector)
            if await options.count() > 0:
                await options.first.click()
                print(f"    ✓ Clicked answer using selector: {selector}")
                answer_clicked = True
                await page.wait_for_timeout(500)
                break

        if not answer_clicked:
            print("    ⚠️  Could not find answer to click")
            return False

        # Try to find and click submit/continue button
        submit_selectors = [
            'button:has-text("Submit")',
            'button[type="submit"]',
            'button:has-text("Continue")',
            'button:has-text("Next")',
            'button[data-testid="core-ui-button"]',
            'svg[data-testid="core-ui-icon-arrow-right"]',
        ]

        button_clicked = False
        for selector in submit_selectors:
            btn = page.locator(selector)
            if await btn.count() > 0 and await btn.first.is_visible():
                await btn.first.click()
                print(f"    ✓ Clicked button: {selector}")
                button_clicked = True
                await page.wait_for_timeout(1500)
                break

        if not button_clicked:
            print("    ⚠️  Could not find submit/continue button")

        return True

    except Exception as e:
        print(f"    ⚠️  Quiz handling error: {e}")
        import traceback
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════════════════════
# 3-tier navigation — ported from Tampermonkey nextClick()
# ═══════════════════════════════════════════════════════════════════════

async def next_click(page) -> str:
    """
    Navigate to the next page using Tampermonkey's 3-tier cascade.

    Tampermonkey tried selectors in this exact order:
      1. p[data-testid="lesson-name"]         → click (advance to next lesson tab)
      2. svg[data-testid="core-ui-icon-arrow-right"] → click parent (next page in lesson)
      3. button[data-testid="core-ui-button"] → click (end-of-lesson CTA / "Continue")

    The Playwright script from tutorial_screenshots.py only tried
    button[aria-label="Next activity"], which caused sudden stops.

    Returns: "lesson" | "next" | "last" | "done" | "error"
    """
    # Check for error page first (avoid infinite retry loops)
    error_text = page.locator('text=/something went wrong/i')
    if await error_text.count() > 0:
        print("    ⚠️  Error page detected - stopping navigation")
        return "error"

    # Tier 1: lesson name tab — advances to next lesson
    lesson_tabs = page.locator('p[data-testid="lesson-name"]')
    if await lesson_tabs.count() > 0:
        try:
            if await lesson_tabs.first.is_visible():
                await lesson_tabs.first.click()
                return "lesson"
        except Exception:
            pass

    # Tier 2: right arrow — next activity within lesson
    arrow = page.locator('svg[data-testid="core-ui-icon-arrow-right"]')
    if await arrow.count() > 0:
        try:
            if await arrow.first.is_visible():
                # Click the parent element (the clickable wrapper), same as TM
                await arrow.first.evaluate("el => el.parentElement.click()")
                return "next"
        except Exception:
            pass

    # Tier 3: end-of-lesson CTA button ("Continue", "Start Quiz", etc.)
    cta = page.locator('button[data-testid="core-ui-button"]')
    if await cta.count() > 0:
        try:
            if await cta.first.is_visible():
                await cta.first.click()
                return "last"
        except Exception:
            pass

    return "done"


# ═══════════════════════════════════════════════════════════════════════
# Login — from tutorial_screenshots.py (unchanged logic)
# ═══════════════════════════════════════════════════════════════════════

async def login(page, phone: str, return_url: str):
    """
    Walk through staging login steps.
    Staging always uses: OTP=123456, license=1234, email=qa@lyft.com
    """
    async def fill_submit(selector: str, value: str, step_name: str = ""):
        if step_name:
            print(f"       {step_name}...")
        await page.locator(selector).first.fill(value)
        await page.locator('button[type="submit"]').first.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

    print("    🔐 Logging in...")

    # Step 1: phone number
    await fill_submit('input[name="phone"]', phone, "Phone")

    # Step 2: OTP - check if field exists first
    otp_field = page.locator('input[name="phoneCode"]')
    if await otp_field.count() > 0:
        await fill_submit('input[name="phoneCode"]', "123456", "OTP")
    else:
        print("       ⚠️  OTP field not found - checking page state...")
        # Take a debug screenshot
        debug_dir = Path.home() / "studio" / "output" / "debug_login"
        debug_dir.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(debug_dir / "after_phone.png"))
        print(f"       Debug screenshot: {debug_dir / 'after_phone.png'}")
        print(f"       Current URL: {page.url}")

        # Check if we're already past OTP (maybe auto-authenticated?)
        if "tutorial" in page.url or "learningcenter" in page.url and "login" not in page.url:
            print("       ✅ Already authenticated!")
            return

    # Step 3: driver's license (may not appear)
    if await page.locator('input[name="drivers_license_number"]').count():
        await fill_submit('input[name="drivers_license_number"]', "1234")

    # Step 4: email (may not appear)
    email_sel = 'input[type="email"], input[name="email"]'
    if await page.locator(email_sel).count():
        await fill_submit(email_sel, "qa@lyft.com")

    # Step 5: Terms of service
    agree_btn = page.get_by_role("button", name="I Agree")
    if await agree_btn.count():
        await agree_btn.click()
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    # If login redirected away from tutorial, navigate back
    if return_url not in page.url:
        await page.goto(return_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

    print("    ✅ Logged in")


# ═══════════════════════════════════════════════════════════════════════
# Capture loop — one locale
# ═══════════════════════════════════════════════════════════════════════

async def capture_locale(
    page,
    base_url: str,
    course_id: str,
    locale: str,
    out_dir: Path,
    phone: str,
    course_structure: dict = None,
) -> list:
    """
    Navigate through every page of a course for one locale and save PNGs.

    URL pattern: {base_url}/tutorial/{course_id}?locale_language={locale}
    Returns list of capture dicts: {page_num, path, status, error?}
    """
    url = f"{base_url}/tutorial/{course_id}?locale_language={locale}"

    print(f"\n  📍 [{locale}] {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass

    # Login if required
    if await page.locator('input[name="phone"]').count():
        await login(page, phone, url)

    await page.wait_for_timeout(2000)

    # Save into out_dir/{locale}/
    locale_folder = out_dir / locale
    locale_folder.mkdir(parents=True, exist_ok=True)

    captures = []
    page_num = 0
    consecutive_failures = 0

    while True:
        page_num += 1

        # ── Apply Tampermonkey fixes ──────────────────────────────────
        video_wait_ms = await prepare_page(page)
        if video_wait_ms > 0:
            print(f"    [{locale}] page {page_num:03d} — video, waiting {video_wait_ms}ms...")
            await page.wait_for_timeout(video_wait_ms)
        else:
            await page.wait_for_timeout(PAGE_SETTLE_MS)

        # ── Screenshot ────────────────────────────────────────────────
        filename = locale_folder / f"{page_num:03d}.png"
        try:
            await page.screenshot(
                path=str(filename),
                full_page=False,          # viewport only — matches TM htmlToImage behavior
                animations="disabled",
            )
            captures.append({
                "page_num": page_num,
                "path": str(filename),
                "status": "ok",
            })
            print(f"    [{locale}] ✓ page {page_num:03d} → {filename.name}")
            consecutive_failures = 0
        except Exception as e:
            print(f"    [{locale}] ✗ page {page_num:03d} screenshot failed: {e}")
            captures.append({
                "page_num": page_num,
                "path": None,
                "status": "error",
                "error": str(e),
            })
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"    [{locale}] {MAX_CONSECUTIVE_FAILURES} consecutive failures — stopping")
                break

        # ── Handle quiz AFTER screenshot ──────────────────────────────
        quiz_handled = await handle_quiz(page, page_num, course_structure)
        if quiz_handled:
            # Quiz was handled, continue to next page
            await page.wait_for_timeout(1000)

        # ── Navigate to next page ─────────────────────────────────────
        action = await next_click(page)
        print(f"    [{locale}] nav → {action}")

        if action == "done":
            print(f"    [{locale}] ✅ end of tutorial ({page_num} pages)")
            break

        if action == "error":
            print(f"    [{locale}] ❌ error page encountered, stopping ({page_num} pages)")
            break

        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:
            pass
        await page.wait_for_timeout(PAGE_SETTLE_MS)

    return captures


# ═══════════════════════════════════════════════════════════════════════
# ZIP + S3 upload
# ═══════════════════════════════════════════════════════════════════════

def zip_and_upload(out_dir: Path, job_id: str) -> str:
    """
    Zip all PNGs under out_dir and upload to S3.
    Returns the S3 key of the uploaded zip.
    """
    import boto3

    zip_path = out_dir.parent / f"{job_id}_screenshots.zip"
    png_files = sorted(out_dir.rglob("*.png"))

    print(f"\n  📦 Zipping {len(png_files)} screenshots...")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for png in png_files:
            # Archive path: locale/001.png
            arc_name = png.relative_to(out_dir)
            zf.write(png, arc_name)

    bucket = os.environ.get("S3_BUCKET", "lyft-lyftlearn-production-iad")
    s3_key = f"course-qa/screenshots/{job_id}_screenshots.zip"

    boto3.client("s3").upload_file(str(zip_path), bucket, s3_key)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"  ✅ Uploaded {size_mb:.1f}MB → s3://{bucket}/{s3_key}")

    return s3_key


# ═══════════════════════════════════════════════════════════════════════
# Async core
# ═══════════════════════════════════════════════════════════════════════

async def _run_async(params: dict) -> dict:
    # Import helpers from runner.py — do not duplicate
    sys.path.insert(0, str(Path.home() / "studio"))
    from slack_bot.runner import _load_env, _get, _post_callback

    _load_env()

    job_id       = _get(params, "job_id",              "JOB_ID",           str(uuid.uuid4())[:8])
    course_id    = _get(params, "course_id",           "COURSE_ID",        required=True)

    # Check both "locales" (new) and "languages" (QA compat) fields
    locales_raw = params.get("locales")
    if locales_raw and isinstance(locales_raw, list):
        # If locales is already a list, use it directly
        locales = locales_raw
    else:
        # Otherwise try as comma-separated string (from "languages" field or env)
        locales_raw = _get(params, "languages", "LANGUAGES", ",".join(DEFAULT_LOCALES))
        if locales_raw.strip().lower() == "all":
            locales = DEFAULT_LOCALES
        else:
            locales = [l.strip() for l in locales_raw.split(",") if l.strip()]

    # Ensure we have at least one locale
    if not locales:
        locales = DEFAULT_LOCALES

    base_url     = _get(params, "staging_base_url",    "STAGING_BASE_URL", required=True)
    phone        = _get(params, "staging_phone",       "STAGING_PHONE",    required=True)
    callback_url = _get(params, "workato_callback_url","WORKATO_CALLBACK_URL", "")
    slack_channel_id = _get(params, "slack_channel_id","SLACK_CHANNEL_ID", "")
    slack_thread_ts  = _get(params, "slack_thread_ts", "SLACK_THREAD_TS",  "")

    out_dir = Path.home() / "studio" / "output" / f"screenshots_{job_id}"
    out_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc)

    callback = {
        "job_id":           job_id,
        "job_type":         "screenshots",
        "course_id":        course_id,
        "status":           "error",
        "locales":          locales,
        "total_pages":      0,
        "per_locale_counts": {},
        "s3_zip_key":       None,
        "duration_seconds": None,
        "slack_channel_id": slack_channel_id,
        "slack_thread_ts":  slack_thread_ts,
        "error":            None,
    }

    print(f"\n{'='*60}")
    print(f"  Screenshot Job: {job_id}")
    print(f"  Course:         {course_id}")
    print(f"  Locales:        {', '.join(locales)}")
    print(f"  Base URL:       {base_url}")
    print(f"  Output:         {out_dir}")
    print(f"{'='*60}")

    try:
        from playwright.async_api import async_playwright

        # Fetch course structure with quiz positions (once for all locales)
        print("\n📚 Fetching course structure from Contentful...")
        course_structure = fetch_course_structure(course_id)

        all_captures: dict[str, list] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",   # critical on SageMaker — /dev/shm is tiny
                    "--disable-gpu",
                ],
            )

            for locale in locales:
                print(f"\n📸 Locale: {locale}")
                context = await browser.new_context(
                    viewport=VIEWPORT,
                    device_scale_factor=DEVICE_SCALE_FACTOR,
                    user_agent=USER_AGENT,
                )
                page = await context.new_page()

                try:
                    captures = await capture_locale(
                        page, base_url, course_id, locale, out_dir, phone, course_structure
                    )
                    all_captures[locale] = captures
                    ok_count = len([c for c in captures if c["status"] == "ok"])
                    print(f"  ✅ {locale}: {ok_count} pages captured")
                except Exception as e:
                    print(f"  ❌ {locale} failed: {e}")
                    traceback.print_exc()
                    all_captures[locale] = [{"status": "error", "error": str(e)}]
                finally:
                    await context.close()

            await browser.close()

        # Zip and upload
        s3_key = zip_and_upload(out_dir, job_id)

        per_locale_counts = {
            loc: len([c for c in caps if c.get("status") == "ok"])
            for loc, caps in all_captures.items()
        }
        total_pages = sum(per_locale_counts.values())

        callback.update({
            "status":            "success",
            "total_pages":       total_pages,
            "per_locale_counts": per_locale_counts,
            "s3_zip_key":        s3_key,
        })

        print(f"\n{'='*60}")
        print(f"  Done! {total_pages} total screenshots across {len(locales)} locale(s)")
        for loc, count in per_locale_counts.items():
            print(f"  {loc}: {count} pages")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\n❌ Screenshot job failed: {e}")
        traceback.print_exc()
        callback["error"] = str(e)

    finally:
        duration = round(
            (datetime.now(timezone.utc) - started_at).total_seconds(), 1
        )
        callback["duration_seconds"] = duration
        print(f"\n⏱️  Duration: {duration}s")
        _post_callback(callback_url, callback)

    return callback


# ═══════════════════════════════════════════════════════════════════════
# Public entry point (called by poller.py)
# ═══════════════════════════════════════════════════════════════════════

def run_screenshots(params: dict = None) -> dict:
    """
    Synchronous wrapper — matches runner.run() interface so poller.py
    can call either interchangeably.
    """
    return asyncio.run(_run_async(params or {}))


# ═══════════════════════════════════════════════════════════════════════
# CLI (manual use on instance)
# ═══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Capture screenshots of a LyftLearn tutorial (all locales)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python screenshot_module/screenshot_runner.py --course 2yQq04tUUk1H67xlZA7PLn
    python screenshot_module/screenshot_runner.py --course 2yQq04tUUk1H67xlZA7PLn --locales es,fr
    python screenshot_module/screenshot_runner.py --course 2yQq04tUUk1H67xlZA7PLn --no-upload
        """,
    )
    parser.add_argument("--course", "-c", required=True, help="Contentful course entry ID")
    parser.add_argument(
        "--locales", "-l",
        default=",".join(DEFAULT_LOCALES),
        help=f"Comma-separated locales (default: {','.join(DEFAULT_LOCALES)})",
    )
    parser.add_argument("--no-upload", action="store_true", help="Skip S3 upload, keep files locally")
    args = parser.parse_args()

    params = {
        "job_id":  f"manual-{args.course[:8]}",
        "course_id": args.course,
        "languages": args.locales,
    }

    if args.no_upload:
        # Monkey-patch to skip upload
        import sys
        sys.modules[__name__].zip_and_upload = lambda folder, job_id: print("  ⏭️  --no-upload: skipping S3") or "local"

    result = run_screenshots(params)
    print(json.dumps({k: v for k, v in result.items() if k != "error"}, indent=2))

    if result.get("status") != "success":
        sys.exit(1)


if __name__ == "__main__":
    main()
