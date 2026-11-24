"""Take README screenshots of a running dashboard (needs: pip install playwright; playwright install chromium).

    python server_and_ui.py --demo --demo-lap 14 --speedup 3 &
    python tools/screenshot.py --url http://127.0.0.1:8050 --wait 40 --out images/dashboard.png
"""
import argparse
import asyncio

from playwright.async_api import async_playwright


async def shoot(url: str, out: str, wait: float, width: int, height: int, chrome: str | None) -> None:
    async with async_playwright() as p:
        kw = {"executable_path": chrome} if chrome else {}
        browser = await p.chromium.launch(**kw)
        page = await browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1.5)
        await page.goto(url)
        await page.wait_for_timeout(wait * 1000)
        await page.screenshot(path=out, full_page=True)
        await browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8050")
    ap.add_argument("--out", default="images/dashboard.png")
    ap.add_argument("--wait", type=float, default=10.0, help="seconds to let charts fill")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--chrome", default=None, help="path to an installed Chrome, if Playwright's isn't downloaded")
    a = ap.parse_args()
    asyncio.run(shoot(a.url, a.out, a.wait, a.width, a.height, a.chrome))
    print(a.out)
