"""Computer 실행 도구에서 네이버 홈페이지 접속과 브라우저 종료를 확인한다."""
import asyncio
import sys
import traceback

from playwright.async_api import async_playwright


async def main() -> None:
    playwright = None
    browser = None
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=False)
        page = await browser.new_page()
        response = await page.goto(
            "https://www.naver.com/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        print(f"접속 URL: {page.url}", flush=True)
        print(f"HTTP 상태: {response.status if response else '응답 없음'}", flush=True)
        print(f"페이지 제목: {await page.title()}", flush=True)
        print("브라우저 창을 15초간 유지합니다.", flush=True)
        await asyncio.sleep(15)
        if response is None or response.status >= 400:
            raise RuntimeError("정상 홈페이지 HTTP 응답을 확인하지 못했습니다.")
    finally:
        try:
            if browser is not None:
                await browser.close()
                print("브라우저 종료 완료", flush=True)
        finally:
            if playwright is not None:
                await playwright.stop()
                print("Playwright 종료 완료", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"실행 실패: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        sys.exit(1)
