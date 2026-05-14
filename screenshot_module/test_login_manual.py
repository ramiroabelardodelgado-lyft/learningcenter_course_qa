#!/usr/bin/env python3
"""Manual login test with detailed debugging"""

import asyncio
import sys
import os
from pathlib import Path

# Add local packages
module_dir = Path(__file__).parent
local_packages = module_dir / ".local-packages"
sys.path.insert(0, str(local_packages))
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(module_dir / ".local-browsers")

from playwright.async_api import async_playwright

PHONE = "4305558360"  # Try without country code
COURSE_ID = "3ADF1isp0prTdHs7vaYQqx"
BASE_URL = "https://www-staging.lyft.net/learningcenter"

async def manual_login_test():
    url = f"{BASE_URL}/tutorial/{COURSE_ID}?locale_language=en"
    debug_dir = Path.home() / "studio" / "output" / "debug_login_manual"
    debug_dir.mkdir(parents=True, exist_ok=True)

    print(f"Testing login to: {url}")
    print(f"Phone: {PHONE}")
    print(f"Debug screenshots: {debug_dir}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )

        page = await browser.new_page(
            viewport={"width": 393, "height": 852},
            device_scale_factor=3,
        )

        # Navigate to tutorial
        print("\n1. Navigating to tutorial...")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(debug_dir / "01_initial.png"))
        print(f"   URL: {page.url}")

        # Check if phone field exists
        phone_field = page.locator('input[name="phone"]')
        if await phone_field.count() > 0:
            print("\n2. Phone field found - filling...")

            # Clear and fill the phone field more carefully
            await phone_field.first.click()
            await page.wait_for_timeout(500)
            await phone_field.first.fill("")  # Clear first
            await page.wait_for_timeout(500)
            await phone_field.first.type(PHONE, delay=100)  # Type slowly
            await page.wait_for_timeout(500)
            await page.screenshot(path=str(debug_dir / "02_phone_filled.png"))

            print(f"   Filled: {PHONE}")

            # Find and click submit button
            print("\n3. Looking for submit button...")
            submit_btn = page.locator('button[type="submit"]')
            if await submit_btn.count() > 0:
                print(f"   Found {await submit_btn.count()} submit button(s)")
                await submit_btn.first.click()
                print("   Clicked submit")
            else:
                # Try alternative selectors
                continue_btn = page.get_by_text("Continue with Phone", exact=False)
                if await continue_btn.count() > 0:
                    await continue_btn.click()
                    print("   Clicked 'Continue with Phone' button")

            # Wait for navigation
            print("\n4. Waiting for response...")
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(debug_dir / "03_after_submit.png"))
            print(f"   URL: {page.url}")

            # Check for OTP field
            otp_field = page.locator('input[name="phoneCode"]')
            if await otp_field.count() > 0:
                print("\n5. ✅ OTP field found! Login is working.")
                await otp_field.first.fill("123456")
                await page.wait_for_timeout(500)
                await page.screenshot(path=str(debug_dir / "04_otp_filled.png"))

                # Submit OTP
                submit_btn = page.locator('button[type="submit"]')
                if await submit_btn.count() > 0:
                    await submit_btn.first.click()
                    print("   Submitted OTP")
                    await page.wait_for_timeout(3000)
                    await page.screenshot(path=str(debug_dir / "05_after_otp.png"))
                    print(f"   URL: {page.url}")
            else:
                print("\n5. ❌ OTP field not found")
                # Check for errors
                error = page.locator('text=/unable to contact/i')
                if await error.count() > 0:
                    print("   ERROR: Phone number rejected")
                else:
                    print("   Checking page content...")
                    title = await page.title()
                    print(f"   Page title: {title}")
        else:
            print("\n2. ✅ No phone field - already authenticated or different flow")
            await page.screenshot(path=str(debug_dir / "02_no_login_needed.png"))

        # Final state
        print(f"\n6. Final URL: {page.url}")
        await page.screenshot(path=str(debug_dir / "99_final.png"))

        await browser.close()

    print(f"\n✅ Test complete. Check screenshots in: {debug_dir}")

if __name__ == "__main__":
    asyncio.run(manual_login_test())
