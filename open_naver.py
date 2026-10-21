"""2단계: 네이버 검색창에 검색어를 입력하고 검색 결과 화면 이동을 확인한다."""
import asyncio

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

NAVER_URL = "https://www.naver.com/"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        try:
            page = await browser.new_page()
            response = await page.goto(NAVER_URL, wait_until="domcontentloaded")
            status = response.status if response else "응답 없음"
            print(f"접속 URL: {page.url}")
            print(f"HTTP 상태: {status}")
            print(f"페이지 제목: {await page.title()}")

            if response is None or response.status >= 400:
                print("정상 홈페이지 응답을 확인하지 못해 검색을 중지합니다.")
                return

            # 사용자가 먼저 보이는 페이지를 확인한다. 빈 입력이면 검색하지 않는다.
            query = (await asyncio.to_thread(
                input,
                "네이버 홈페이지가 정상일 때만 검색어를 입력하세요.\n"
                "CAPTCHA·접근 제한·추가 인증 화면이면 빈 입력으로 중지하세요: ",
            )).strip()
            if not query:
                print("검색하지 않고 중지했습니다.")
                return

            search_box = page.locator("#query")
            await search_box.fill(query, timeout=10_000)
            await search_box.press("Enter", timeout=10_000)
            await page.wait_for_url(
                "https://search.naver.com/**",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            print(f"이동 후 URL: {page.url}")
            print(f"이동 후 페이지 제목: {await page.title()}")
            print("URL 이동만 확인했습니다. 정상 검색 결과인지는 브라우저에서 확인하세요.")
            print("CAPTCHA·접근 제한·추가 인증이 보이면 종료하고 알려 주세요.")
        except PlaywrightError as exc:
            print(f"브라우저 작업을 중지했습니다: {exc}")
        finally:
            # input()은 대기 중 이벤트 루프를 멈추므로 별도 스레드에서 실행한다.
            try:
                await asyncio.to_thread(
                    input, "브라우저를 확인한 뒤 Enter를 누르면 종료합니다..."
                )
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
