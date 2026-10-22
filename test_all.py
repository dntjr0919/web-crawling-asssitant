"""전체 기능 검증 테스트 (1. 단위 테스트 / 2. 고정 HTML 테스트 / 3. GUI 위젯·호환성 테스트)."""
from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright

from crawler_app.article import extract_article, unsupported_url_reason
from crawler_app.config import load_config
from crawler_app.gui import (
    LogBridge,
    MainWindow,
    format_detail_text,
    resolve_openable_http_url,
)
from crawler_app.models import ResultItem, SiteRunResult, save_job_csv
from crawler_app.nlp import extract_keywords, summarize_text
from crawler_app.runner import Runner, _apply_snippet_keywords
from crawler_app.selector import recommend_for_query
from crawler_app.sites import google, naver


class Test1_UnitTests(unittest.TestCase):
    """[1. 단위 테스트] NLP 정제, URL 판별, 상세 화면 분리, 링크 열기 우선순위, 하위 호환성."""

    def test_nlp_filters_ads_toc_and_boilerplate(self) -> None:
        title = "'가을이 저물기 전에' 가봐야 할 수목원···산림청, 한택식물원"
        dirty_body = (
            "경기 용인시 한택식물원. 산림청 제공\n"
            "한국로또 큰일났다..\"1등 당첨자 찾아가보니..\n"
            "“죽을 것 같은 허리통증,다리저림…新기술로 단 10분?”\n"
            "목차\n"
            "오늘의 콘텐츠 미리보기\n"
            "충청북도 호수 전망 케이블카 & 스카이워크 당일치기 여행 | 서울 출발 합승 차량 이용 | 두 가지 옵션 제공\n"
            "꽃과 나무가 가득한 수목원은 가을이면 저마다 다른 빛깔로 물든다.\n"
            "산림청은 22일 올해 선정한 꼭 가봐야 할 수목원 10선 중 단풍과 가을꽃을 감상할 수 있는 전국 수목원 5곳을 추천했다.\n"
            "첫 번째 추천 장소는 경기 용인시 한택식물원이며 단풍 페스티벌을 개최한다.\n"
            "무단 전재 및 재배포 금지 test@news.kr"
        )
        kws = extract_keywords(dirty_body, title=title, top_k=5)
        summary = summarize_text(dirty_body, title=title, max_sentences=2)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertNotIn("로또", summary)
        self.assertNotIn("허리통증", summary)
        self.assertNotIn("두 가지 옵션 제공", summary)
        self.assertNotIn("무단 전재", summary)
        self.assertIn("수목원", summary)
        self.assertIn("수목원", kws)

    def test_unsupported_url_classification(self) -> None:
        self.assertIsNotNone(unsupported_url_reason("https://www.youtube.com/watch?v=rO3x8cuW97M"))
        self.assertIsNotNone(unsupported_url_reason("https://www.youtube.com/channel/UCifJj8SKS7F7tQ7XVUeHkFA"))
        self.assertIsNotNone(unsupported_url_reason("https://www.instagram.com/p/DeTWDz2k5ZZ/"))
        self.assertIsNotNone(unsupported_url_reason("http://www.openstreetmap.org/copyright"))
        self.assertIsNotNone(unsupported_url_reason("https://example.go.kr/files/guide.pdf"))
        self.assertIsNone(unsupported_url_reason("https://smart.science.go.kr/board/view.action?dataSid=8119"))

    def test_snippet_keywords_never_fake_body_or_summary(self) -> None:
        item = ResultItem(
            query="금요일 나들이",
            source="naver",
            source_name="네이버",
            title="수원박물관 10월 주말 문화행사 안내",
            search_link="https://smuseum.suwon.go.kr/board/1",
            original_link="https://smuseum.suwon.go.kr/board/1",
            description="금요일 오후 가족과 함께 즐길 수 있는 수원박물관 야간 개장 및 체험 안내입니다.",
            search_type="web",
        )
        _apply_snippet_keywords(item, "failed")
        self.assertIsNone(item.body_text)
        self.assertIsNone(item.summary)
        self.assertEqual(item.summary_source, "none")
        self.assertEqual(item.keywords_source, "title_description")
        self.assertEqual(item.processing["keywords"], "extracted_from_snippet")
        self.assertEqual(item.processing["summary"], "failed")

    def test_detail_view_separation_news_vs_general(self) -> None:
        news_item = ResultItem(
            query="경제",
            source="naver",
            source_name="네이버",
            title="한국은행 기준금리 동결",
            search_link="https://news.example.com/1",
            original_link="https://news.example.com/1",
            portal_link="https://n.news.naver.com/1",
            press="연합뉴스",
            date_text="1시간 전",
            search_type="news",
            body_status="extracted",
            body_text="한국은행이 기준금리를 동결했다.",
            keywords=["한국은행", "기준금리"],
            keywords_source="body",
            summary="한국은행이 기준금리를 동결했다.",
            summary_source="body",
        ).to_dict()

        web_item = ResultItem(
            query="금요일 가볼만한 곳",
            source="naver",
            source_name="네이버",
            title="10월 운영 안내 - 수원박물관",
            search_link="https://smuseum.suwon.go.kr/sw/board/read?boardNo=2847",
            original_link="https://smuseum.suwon.go.kr/sw/board/read?boardNo=2847",
            press="수원박물관",
            domain="smuseum.suwon.go.kr",
            date_text="2026.10.15.",
            description="수원박물관 10월 관람 및 교육 프로그램 안내입니다.",
            search_type="web",
            body_status="failed",
            body_error="본문 영역을 특정하지 못함",
            keywords=["수원박물관", "운영", "안내"],
            keywords_source="title_description",
            summary=None,
            summary_source="none",
        ).to_dict()

        news_text = format_detail_text(news_item, default_search_type="news")
        self.assertIn("[뉴스 상세 보기]", news_text)
        self.assertIn("언론사: 연합뉴스", news_text)
        self.assertIn("포털 기사 링크: https://n.news.naver.com/1", news_text)
        self.assertIn("핵심 키워드 [입력 출처: 본문 기준]:", news_text)

        web_text = format_detail_text(web_item, default_search_type="web")
        self.assertIn("[웹 문서 (일반형) 상세 보기]", web_text)
        self.assertIn("사이트명 / 도메인: 수원박물관 (smuseum.suwon.go.kr)", web_text)
        self.assertNotIn("포털 기사 링크:", web_text)
        self.assertIn("게시일(검색 결과 표시): 2026.10.15.", web_text)
        self.assertIn("수집 시각(앱 수집):", web_text)
        self.assertIn("핵심 키워드 [입력 출처: 제목 + 검색 설명 기준 (본문 아님)]:", web_text)
        self.assertIn("요약 [입력 출처: 생성 안 됨 (본문 없음)]:", web_text)
        self.assertIn("(주요 내용을 추출하지 못했습니다 — 사유: 본문 영역을 특정하지 못함)", web_text)

    def test_resolve_openable_http_url(self) -> None:
        # 1) 유효한 원문 URL 우선
        u1, lbl1 = resolve_openable_http_url({
            "original_link": "https://smart.science.go.kr/board/view.action?dataSid=8119",
            "search_link": "https://search.naver.com/search.naver?query=test",
        })
        self.assertEqual(u1, "https://smart.science.go.kr/board/view.action?dataSid=8119")
        self.assertEqual(lbl1, "원문 URL")

        # 2) 원문 URL이 없으면 검색 결과 링크로 대체
        u2, lbl2 = resolve_openable_http_url({
            "original_link": None,
            "search_link": "https://news.naver.com/main/read.naver?oid=001",
        })
        self.assertEqual(u2, "https://news.naver.com/main/read.naver?oid=001")
        self.assertEqual(lbl2, "검색 결과 링크")

        # 3) http/https가 아닌 잘못된 스킴 거부
        u3, _ = resolve_openable_http_url({
            "original_link": "javascript:alert(1)",
            "search_link": "file:///C:/Windows/System32/cmd.exe",
        })
        self.assertIsNone(u3)

    def test_backward_compatibility_with_saved_json(self) -> None:
        # 기존 스키마(domain, keywords_source, summary_source 필드가 없는 JSON) 호환 확인
        legacy_item = {
            "query": "금요일 가볼만한 곳",
            "source": "naver",
            "source_name": "네이버",
            "title": "기존 저장 항목 제목",
            "search_link": "https://example.com/post/1",
            "original_link": "https://example.com/post/1",
            "portal_link": None,
            "description": "검색 설명입니다.",
            "date_text": "2026.10.20.",
            "press": None,
            "collected_at": "2026-10-22T14:25:53+09:00",
            "extraction_method": "naver_link_heuristic",
            "status": "collected",
            "error": None,
            "body_text": None,
            "body_status": "failed",
            "body_error": "본문 없음",
            "body_final_url": "https://example.com/post/1",
            "body_method": None,
            "body_collected_at": None,
            "keywords": ["금요일", "제목"],
            "summary": None,
            "processing": {"body": "failed", "keywords": "extracted_from_snippet", "summary": "failed"},
        }
        formatted = format_detail_text(legacy_item, default_search_type="web")
        self.assertIn("사이트명 / 도메인: example.com", formatted)
        self.assertIn("핵심 키워드 [입력 출처: 제목 + 검색 설명 기준 (본문 아님)]:", formatted)

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = Path(tmpdir) / "legacy.csv"
            save_job_csv({"query": "테스트", "search_type": "web", "sites": [{"items": [legacy_item]}]}, csv_path)
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)


class Test2_FixedHTMLPlaywrightTests(unittest.IsolatedAsyncioTestCase):
    """[2. 고정 HTML 테스트] 일반 웹페이지 본문 추출·노이즈 정제, 네이버 웹 목록 정제('새 창 열림'/저작권 링크 제외), 상태 구분."""

    async def test_naver_web_extract_js_cleans_title_and_excludes_copyright(self) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                naver_web_html = """
                <html><body>
                  <div id="main_pack">
                    <div class="total_wrap">
                      <div class="source_box"><span class="name">수원박물관</span><span class="sub_time">2026.10.15.</span></div>
                      <a class="link_tit" href="https://smuseum.suwon.go.kr/sw/board/read?boardNo=2847">
                        10월 운영 안내 - 수원박물관<span class="blind">새 창 열림</span>
                      </a>
                      <div class="total_dsc">수원박물관 10월 주말 가족 문화 프로그램 및 운영 시간 안내입니다.</div>
                    </div>
                    <div class="total_wrap">
                      <a class="link_tit" href="https://smartravel.kr/guides/KR/82839">
                        상무금요시장 — 광주 가볼만한 곳새 창 열림
                      </a>
                      <div class="total_dsc">광주 서구 상무금요시장 먹거리와 볼거리 코스 안내.</div>
                    </div>
                    <div class="api_ly_map">
                      <a href="http://www.openstreetmap.org/copyright">/OpenStreetMap</a>
                    </div>
                  </div>
                </body></html>
                """
                await page.set_content(naver_web_html)
                rows = await page.evaluate(naver.EXTRACT_JS, {"max": 10, "searchType": "web"})
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["title"], "10월 운영 안내 - 수원박물관")
                self.assertEqual(rows[0]["press"], "수원박물관")
                self.assertEqual(rows[0]["date_text"], "2026.10.15.")
                self.assertIn("수원박물관 10월 주말", rows[0]["description"])
                self.assertEqual(rows[1]["title"], "상무금요시장 — 광주 가볼만한 곳")
                # OpenStreetMap 저작권 링크가 제외되었는지 확인
                hrefs = [r["href"] for r in rows]
                self.assertNotIn("http://www.openstreetmap.org/copyright", hrefs)
            finally:
                await browser.close()

    async def test_general_web_page_main_content_and_noise_removal(self) -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                # 기관 게시판 / 관광 안내형 일반 웹페이지 (뉴스 선택자 없이 #contents 및 커스텀 div 사용 + 메뉴/광고/관련글 포함)
                web_page_html = """
                <html><body>
                  <header><nav><ul><li><a href="/1">메뉴1</a></li><li><a href="/2">메뉴2</a></li></ul></nav></header>
                  <div id="container">
                    <aside class="snb_menu"><ul><li><a href="/a">사이드메뉴A</a></li><li><a href="/b">사이드메뉴B</a></li></ul></aside>
                    <div class="custom_guide_detail" style="width:800px; height:400px;">
                      <h1>삼척어린이과학체험관 10월 주말 운영 안내</h1>
                      <div class="ad_banner">한국로또 큰일났다.."1등 당첨자 찾아가보니..</div>
                      <div class="guide_txt">
                        삼척어린이과학체험관은 금요일과 주말에 방문하는 가족 관람객을 위해 상설 전시실과 천체투영관을 특별 운영합니다.
                        관람 시간은 오전 9시 30분부터 오후 5시 30분까지이며, 매주 금요일 오후에는 어린이 과학 실험 교실이 무료로 진행됩니다.
                        사전 예약은 공식 홈페이지 통합예약 시스템에서 신청할 수 있으며 현장 발권도 일부 가능합니다.
                      </div>
                      <div class="related_posts">
                        <ul>
                          <li><a href="/r1">관련 글 1: 강원도 주말 나들이 명소 추천</a></li>
                          <li><a href="/r2">관련 글 2: 동해안 가족 여행 코스 베스트</a></li>
                          <li><a href="/r3">관련 글 3: 삼척 해상케이블카 이용 요금</a></li>
                          <li><a href="/r4">관련 글 4: 아이와 가볼만한 과학관 모음</a></li>
                        </ul>
                      </div>
                    </div>
                  </div>
                  <footer>저작권자(c) 삼척시청 무단 전재 및 재배포 금지</footer>
                </body></html>
                """
                await page.route(
                    "https://test.local/web-guide",
                    lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=web_page_html),
                )
                res = await extract_article(page, "https://test.local/web-guide", 10_000)
                self.assertEqual(res.status, "extracted")
                self.assertIn("삼척어린이과학체험관은 금요일과 주말에 방문하는", res.text or "")
                # 메뉴·광고·관련 글·푸터가 본문에서 제거되었는지 확인
                self.assertNotIn("사이드메뉴A", res.text or "")
                self.assertNotIn("한국로또", res.text or "")
                self.assertNotIn("관련 글 1", res.text or "")
                self.assertNotIn("무단 전재", res.text or "")

                # 미지원 URL 상태(unsupported) 확인
                res_unsup = await extract_article(page, "https://www.youtube.com/watch?v=rO3x8cuW97M", 10_000)
                self.assertEqual(res_unsup.status, "unsupported")

                # 접근 제한 상태(blocked) 확인
                await page.route(
                    "https://test.local/blocked",
                    lambda route: route.fulfill(status=403, content_type="text/html; charset=utf-8", body="<html><body>Forbidden</body></html>"),
                )
                res_blk = await extract_article(page, "https://test.local/blocked", 10_000)
                self.assertEqual(res_blk.status, "blocked")
            finally:
                await browser.close()


class Test3_GUIWidgetTests(unittest.TestCase):
    """[3. GUI 위젯 테스트] 결과 행 선택·토글, 패널 레이아웃 프리셋/개별 제어, 링크 열기 버튼 분리, 저장 폴더 경로 반영 확인."""

    def test_gui_result_row_open_button_and_saved_dir_tracking(self) -> None:
        from PySide6.QtWidgets import QApplication, QPushButton

        app = QApplication.instance() or QApplication([])
        cfg = load_config()
        runner = Runner(cfg)
        bridge = LogBridge()
        win = MainWindow(cfg, runner, bridge)
        try:
            sample_site = {
                "site_id": "naver",
                "site_name": "네이버",
                "state": "done",
                "items": [
                    ResultItem(
                        query="금요일 나들이",
                        source="naver",
                        source_name="네이버",
                        title="수원박물관 주말 안내",
                        search_link="https://smuseum.suwon.go.kr/board/1",
                        original_link="https://smuseum.suwon.go.kr/board/1",
                        press="수원박물관",
                        domain="smuseum.suwon.go.kr",
                        search_type="web",
                        body_status="extracted",
                        body_text="수원박물관 주말 프로그램 본문 내용입니다.",
                        keywords=["수원박물관", "주말"],
                        keywords_source="body",
                        summary="수원박물관 주말 프로그램 본문 내용입니다.",
                        summary_source="body",
                    ).to_dict(),
                    ResultItem(
                        query="금요일 나들이",
                        source="naver",
                        source_name="네이버",
                        title="과천과학관 특별전 안내",
                        search_link="https://www.sciencecenter.go.kr/board/2",
                        original_link="https://www.sciencecenter.go.kr/board/2",
                        press="국립과천과학관",
                        domain="www.sciencecenter.go.kr",
                        search_type="web",
                        body_status="extracted",
                        body_text="과천과학관 특별전 본문 내용입니다.",
                        keywords=["과천과학관", "특별전"],
                        keywords_source="body",
                        summary="과천과학관 특별전 본문 내용입니다.",
                        summary_source="body",
                    ).to_dict(),
                ],
            }
            win._on_site_finished(sample_site)
            self.assertEqual(win.result_table.rowCount(), 2)
            # 6번 열에 '링크 열기' QPushButton 위젯이 생성되었는지 확인
            btn_widget = win.result_table.cellWidget(0, 6)
            self.assertIsInstance(btn_widget, QPushButton)
            self.assertEqual(btn_widget.text(), "링크 열기")

            # [즉시 모드 검증: animations_enabled = False]
            win.animations_enabled = False

            # [패널 2 행 클릭 토글 검증]
            # 1) 0번 행 첫 클릭 -> 3번 패널에 0번 행 상세 내용 표시 및 열림 유지
            win._on_result_cell_clicked(0, 1)
            self.assertFalse(win.panel_3.isHidden())
            self.assertEqual(win._active_detail_row, 0)
            detail_content = win.detail.toPlainText()
            self.assertIn("[웹 문서 (일반형) 상세 보기]", detail_content)
            self.assertIn("사이트명 / 도메인: 수원박물관 (smuseum.suwon.go.kr)", detail_content)

            # 2) 이미 상세가 표시된 0번 행을 다시 클릭 -> 3번 패널이 토글로 닫힘(숨김)
            win._on_result_cell_clicked(0, 1)
            self.assertTrue(win.panel_3.isHidden())

            # 3) 닫힌 상태에서 다른 행(1번 행) 클릭 -> 1번 행 상세 내용이 출력되며 3번 패널이 다시 열림
            win._on_result_cell_clicked(1, 1)
            self.assertFalse(win.panel_3.isHidden())
            self.assertEqual(win._active_detail_row, 1)
            self.assertIn("과천과학관 특별전 안내", win.detail.toPlainText())

            # 4) 1번 행이 열려 있는 상태에서 0번 행 클릭 -> 닫히지 않고 0번 행 상세 내용으로 전환되며 열림 유지
            win._on_result_cell_clicked(0, 1)
            self.assertFalse(win.panel_3.isHidden())
            self.assertEqual(win._active_detail_row, 0)
            self.assertIn("수원박물관 주말 안내", win.detail.toPlainText())

            # [우측 상단 패널 형태 프리셋 버튼 검증]
            # 1,2번 패널 활성화
            win.preset_buttons["1_2"].click()
            self.assertFalse(win.panel_1.isHidden())
            self.assertFalse(win.panel_2.isHidden())
            self.assertTrue(win.panel_3.isHidden())
            self.assertTrue(win.panel_4.isHidden())

            # 2,4번 패널 활성화
            win.preset_buttons["2_4"].click()
            self.assertTrue(win.panel_1.isHidden())
            self.assertFalse(win.panel_2.isHidden())
            self.assertTrue(win.panel_3.isHidden())
            self.assertFalse(win.panel_4.isHidden())

            # 전체 활성화
            win.preset_buttons["all"].click()
            for pid in (1, 2, 3, 4):
                self.assertFalse(win.panels[pid].isHidden())

            # [각 패널 우측 상단 전체/기본/숨기기 버튼 검증]
            # 2번 패널 '전체' 클릭 -> 2번 패널만 활성화
            win.panel_controls[2]["full"].click()
            self.assertTrue(win.panel_1.isHidden())
            self.assertFalse(win.panel_2.isHidden())
            self.assertTrue(win.panel_3.isHidden())
            self.assertTrue(win.panel_4.isHidden())

            # 2번 패널 '기본' 클릭 -> 1~4번 모두 함께 보임
            win.panel_controls[2]["default"].click()
            for pid in (1, 2, 3, 4):
                self.assertFalse(win.panels[pid].isHidden())

            # 4번 패널 '숨기기' 클릭 -> 4번 패널만 숨김
            win.panel_controls[4]["hide"].click()
            self.assertFalse(win.panel_1.isHidden())
            self.assertFalse(win.panel_2.isHidden())
            self.assertFalse(win.panel_3.isHidden())
            self.assertTrue(win.panel_4.isHidden())

            # 저장 폴더 기본값 및 다른 위치 내보내기 시 경로 반영 확인
            self.assertEqual(win._resolve_target_results_dir(), cfg.results_dir.resolve())
            custom_dir = Path(tempfile.gettempdir()).resolve()
            win._last_saved_dir = custom_dir
            self.assertEqual(win._resolve_target_results_dir(), custom_dir)
        finally:
            win.close()
            runner.shutdown()

    def test_detail_panel_sliding_animation(self) -> None:
        """[미닫이 애니메이션 검증] QVariantAnimation 구동, 단계별 크기 변화, 완료 시 닫힘 확인."""
        from PySide6.QtCore import QVariantAnimation
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        cfg = load_config()
        runner = Runner(cfg)
        bridge = LogBridge()
        win = MainWindow(cfg, runner, bridge)
        try:
            win.resize(1200, 700)
            win.show()
            self.assertTrue(win.animations_enabled)

            # 1) 열기 미닫이 애니메이션 테스트
            win.panel_3.setVisible(False)
            win.slide_panel_3(True, animated=True)
            self.assertEqual(win._detail_anim.state(), QVariantAnimation.State.Running)
            self.assertEqual(win._detail_anim.endValue(), 480)
            # 애니메이션 지속시간(220ms) 대기
            QTest.qWait(260)
            self.assertFalse(win.panel_3.isHidden())
            self.assertGreater(win.middle_splitter.sizes()[1], 100)

            # 2) 닫기 미닫이 애니메이션 테스트
            win.slide_panel_3(False, animated=True)
            self.assertEqual(win._detail_anim.state(), QVariantAnimation.State.Running)
            self.assertEqual(win._detail_anim.endValue(), 0)
            QTest.qWait(260)
            self.assertTrue(win.panel_3.isHidden())
        finally:
            win.close()
            runner.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
