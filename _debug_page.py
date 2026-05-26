import asyncio
from playwright.async_api import async_playwright

async def check():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=['--ignore-certificate-errors'])
        page = await browser.new_page(ignore_https_errors=True)
        await page.goto('https://tzzwy.capcloud.com.cn:8443/login', wait_until='load', timeout=30000)
        await page.wait_for_timeout(5000)

        # Check the tab element itself
        tab_info = await page.evaluate("""() => {
            const tabs = document.querySelectorAll('.ivu-tabs-tab');
            return Array.from(tabs).map((t, i) => {
                const style = window.getComputedStyle(t);
                return {
                    index: i,
                    text: t.textContent.trim(),
                    display: style.display,
                    visibility: style.visibility,
                    opacity: style.opacity,
                    rect: t.getBoundingClientRect(),
                    tag: t.tagName,
                    innerText: t.innerText
                };
            });
        }""")

        for t in tab_info:
            print(f'  Tab {t["index"]}: text="{t["text"]}" display={t["display"]} vis={t["visibility"]} '
                  f'rect={t["rect"]}')

        # Try clicking with Playwright
        print("\nTrying Playwright click on tab 2 ...")
        try:
            await page.locator('.ivu-tabs-tab').nth(2).click(timeout=10000)
            print("  Click succeeded")
        except Exception as e:
            print(f"  Click failed: {str(e)[:100]}")
            # Try with force
            try:
                await page.locator('.ivu-tabs-tab').nth(2).click(force=True, timeout=10000)
                print("  Force click succeeded")
            except Exception as e2:
                print(f"  Force click failed: {str(e2)[:100]}")

        await page.wait_for_timeout(3000)

        # Check if input appeared
        inputs = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('input[placeholder]'))
                .filter(i => i.offsetParent !== null)
                .map(i => i.placeholder);
        }""")
        print(f"\nVisible inputs after click: {inputs}")

        # Try typing directly
        try:
            input_field = page.locator('input[placeholder*="手机号"]')
            count = await input_field.count()
            print(f"\ninput[placeholder*='手机号'] found: {count}")
            if count > 0:
                await input_field.first.fill("13800138000", timeout=5000)
                print("  Filled successfully")
        except Exception as e:
            print(f"  Fill failed: {str(e)[:200]}")

        await browser.close()

asyncio.run(check())
