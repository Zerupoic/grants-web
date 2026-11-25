"""Download the grants.gov (simpler.grants.gov) DOT search results as a CSV.

Saves to grants_data/grantsgov_YYYYMMDD.csv. Exits non-zero on failure so a
CI run stops instead of publishing stale data.
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

SEARCH_URL = "https://simpler.grants.gov/search?utm_source=Grants.gov&topLevelAgency=DOT&agency="
DATA_DIR = "grants_data"


async def download_grants_csv(download_path: str = DATA_DIR) -> bool:
    Path(download_path).mkdir(parents=True, exist_ok=True)
    abs_download_path = os.path.abspath(download_path)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            print(f"Opening URL: {SEARCH_URL}")
            await page.goto(SEARCH_URL, wait_until="networkidle")

            print("Waiting for Export button...")
            export_button = page.locator(
                'button[data-testid="button"]:has-text("Export results as .csv")'
            )
            await export_button.wait_for(timeout=15000)

            print("Export button found! Clicking...")
            async with page.expect_download() as download_info:
                await export_button.click()
            download = await download_info.value

            temp_path = os.path.join(abs_download_path, download.suggested_filename)
            await download.save_as(temp_path)

            new_filename = f"grantsgov_{datetime.now().strftime('%Y%m%d')}.csv"
            new_file_path = os.path.join(abs_download_path, new_filename)
            # os.replace overwrites an existing file (same-day re-run), os.rename does not on Windows
            os.replace(temp_path, new_file_path)

            size = os.path.getsize(new_file_path)
            print(f"Download successful: {new_file_path} ({size:,} bytes)")
            return True

        except Exception as e:
            print(f"Error: {e}")
            return False

        finally:
            await context.close()
            await browser.close()
            print("Browser closed.")


def main() -> None:
    ok = asyncio.run(download_grants_csv())
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
