
import asyncio
import os
import sys
from playwright.async_api import async_playwright

async def run():
    print("Testing Playwright launch...")
    async with async_playwright() as p:
        # 1. Try default
        try:
            print("\n1. Testing default launch...")
            browser = await p.chromium.launch()
            print("✓ Default launch success!")
            await browser.close()
        except Exception as e:
            print(f"✗ Default launch failed: {e}")

        # 2. Try explicit path
        chrome_path = '/ms-playwright/chromium-1179/chrome-linux/chrome'
        if os.path.exists(chrome_path):
            print(f"\n2. Testing explicit path: {chrome_path}")
            try:
                browser = await p.chromium.launch(executable_path=chrome_path)
                print("✓ Explicit path success!")
                await browser.close()
            except Exception as e:
                print(f"✗ Explicit path failed: {e}")
        else:
            print(f"\n2. Path NOT found: {chrome_path}")

        # 3. Environment check
        print("\n3. Environment Check:")
        print(f"PYTHONUNBUFFERED: {os.environ.get('PYTHONUNBUFFERED')}")
        print(f"DISPLAY: {os.environ.get('DISPLAY')}")
        print(f"PLAYWRIGHT_BROWSERS_PATH: {os.environ.get('PLAYWRIGHT_BROWSERS_PATH')}")
        
if __name__ == "__main__":
    asyncio.run(run())
