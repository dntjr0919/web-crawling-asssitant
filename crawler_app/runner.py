"""비동기 실행부와 오케스트레이터.

구조:
- 전용 스레드 하나에서 asyncio 이벤트 루프를 계속 실행한다.
  (Windows Python 3.8+ 기본 루프는 ProactorEventLoop이며 Playwright가 요구하는 subprocess를 지원)
- Playwright 객체는 이 스레드에서만 생성·사용·종료한다. GUI는 직접 접근하지 않는다.
- GUI → 실행부: asyncio.run_coroutine_threadsafe / loop.call_soon_threadsafe
- 실행부 → GUI: Qt 시그널 (GUI 쪽에서 QueuedConnection으로 연결)

취소:
- 중지 요청 시 취소 이벤트를 켜고, 사용자 대기 중인 작업은 즉시 깨운다.
- 진행 중인 Playwright 호출은 태스크 cancel()을 쓰지 않는다(공식 문서상 미정의 동작).
  대신 이 앱이 연 브라우저 컨텍스트를 닫아 호출이 오류로 끝나게 한 뒤 정리한다.
- 브라우저 실행(launch) 도중에는 실행이 끝난 직후 취소를 확인하므로 즉시 멈추지 않을 수 있다.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .browser import BrowserManager, ProfileInUseError
from .article import extract_article, valid_article_url
from .config import AppConfig
from .models import STATE_LABELS, SiteRunResult, now_iso
from .sites import SITES
from .sites.common import SiteBlocked, SiteError

logger = logging.getLogger(__name__)
site_logger = logging.getLogger("crawler_app.site")

ACTIVE_STATES = ("queued", "running", "needs_user")


class JobCancelled(Exception):
    pass


@dataclass
class _Job:
    query: str
    site_ids: list[str]
    cancel_event: asyncio.Event
    results: dict[str, SiteRunResult]
    started_at: str = field(default_factory=now_iso)
    waiting: dict[str, asyncio.Event] = field(default_factory=dict)
    browser: BrowserManager | None = None
    extract_body: bool = False
    body_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class SiteRunContext:
    """사이트 모듈에 전달되는 실행 도구. 취소 확인·상태 보고·사용자 대기를 담당한다."""

    def __init__(self, runner: "Runner", job: _Job, result: SiteRunResult) -> None:
        self._runner = runner
        self._job = job
        self._result = result
        cfg = runner.config
        self.max_results = cfg.max_results_per_site
        self.action_timeout_ms = cfg.action_timeout_ms
        self.navigation_timeout_ms = cfg.navigation_timeout_ms
        self.result_wait_timeout_ms = cfg.result_wait_timeout_ms
        self._manual_timeout = cfg.manual_wait_timeout_sec
        self._max_rounds = cfg.max_manual_rounds

    @property
    def final_url(self) -> str | None:
        return self._result.final_url

    @final_url.setter
    def final_url(self, value: str | None) -> None:
        self._result.final_url = value

    def checkpoint(self, step: str) -> None:
        if self._job.cancel_event.is_set():
            raise JobCancelled()
        self._runner._set_state(self._result, "running", step)

    def info(self, message: str) -> None:
        site_logger.info("[%s] %s", self._result.site_name, message)

    def warning(self, message: str) -> None:
        site_logger.warning("[%s] %s", self._result.site_name, message)

    async def request_user(self, message: str) -> None:
        """사용자가 브라우저를 조작하는 동안 자동화를 멈추고 '계속' 또는 중지를 기다린다."""
        if self._job.cancel_event.is_set():
            raise JobCancelled()
        site_id = self._result.site_id
        event = asyncio.Event()
        self._job.waiting[site_id] = event
        self._runner._set_state(self._result, "needs_user", message)
        self._runner._emit_waiting(self._job)
        cancel_wait = asyncio.create_task(self._job.cancel_event.wait())
        continue_wait = asyncio.create_task(event.wait())
        try:
            done, _ = await asyncio.wait(
                {cancel_wait, continue_wait},
                timeout=self._manual_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            # 여기서 취소하는 것은 asyncio 대기 태스크뿐이며 Playwright 호출이 아니다.
            for task in (cancel_wait, continue_wait):
                if not task.done():
                    task.cancel()
            self._job.waiting.pop(site_id, None)
            self._runner._emit_waiting(self._job)
        if self._job.cancel_event.is_set():
            raise JobCancelled()
        if continue_wait not in done:
            raise SiteError(f"사용자 확인 대기 시간({self._manual_timeout}초)을 넘겨 중지했습니다")
        self._runner._set_state(self._result, "running", "계속 요청 수신 - 화면 상태 재확인")

    async def ensure_accessible(self, page: Any, detector: Callable[[Any], Any],
                                first_reason: str | None = None) -> None:
        """제한 화면이면 사용자 확인을 요청하고, '계속' 후 다시 판정한다. 횟수 제한 있음."""
        reason = first_reason if first_reason is not None else await detector(page)
        rounds = 0
        while reason:
            rounds += 1
            if rounds > self._max_rounds:
                raise SiteBlocked(f"{reason} (사용자 확인 {self._max_rounds}회 후에도 계속됨)")
            self.warning(reason)
            await self.request_user(
                f"{reason}. 자동 우회는 하지 않습니다. 브라우저에서 직접 확인한 뒤 '계속'을 누르거나 중지하세요."
            )
            reason = await detector(page)


class Runner(QObject):
    site_state = Signal(str, str, str)      # site_id, state, message
    site_finished = Signal(object)          # SiteRunResult dict
    job_finished = Signal(object)           # 작업 전체 dict
    busy_changed = Signal(bool)
    waiting_changed = Signal(object)        # 사용자 확인 대기 중인 site_id 목록

    def __init__(self, config: AppConfig,
                 playwright_factory: Callable[[], Any] = async_playwright,
                 sites: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.config = config
        self._playwright_factory = playwright_factory
        self._sites = sites if sites is not None else SITES
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._job: _Job | None = None                       # 루프 스레드에서만 접근
        self._job_future: concurrent.futures.Future | None = None
        self._closed = False

    # ---------- GUI 스레드에서 호출 ----------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="automation-runner",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("실행부 이벤트 루프를 시작하지 못했습니다")

    def is_busy(self) -> bool:
        return self._job_future is not None and not self._job_future.done()

    def submit_job(self, query: str, site_ids: list[str],
                   extract_body: bool = False) -> tuple[bool, str]:
        if self._loop is None or self._closed:
            return False, "실행부가 준비되지 않았습니다"
        if self.is_busy():
            return False, "이미 실행 중입니다"
        query = query.strip()
        if not query:
            return False, "검색어를 입력하세요"
        unknown = [s for s in site_ids if s not in self._sites]
        if not site_ids or unknown:
            return False, f"사이트 선택을 확인하세요 {unknown or ''}".strip()
        self._job_future = asyncio.run_coroutine_threadsafe(
            self._run_job(query, list(site_ids), extract_body), self._loop)
        self._job_future.add_done_callback(self._on_job_done)
        self.busy_changed.emit(True)
        return True, "실행 요청됨"

    def request_cancel(self) -> None:
        if self._loop is not None and not self._closed:
            asyncio.run_coroutine_threadsafe(self._cancel(), self._loop)

    def request_continue(self) -> None:
        if self._loop is not None and not self._closed:
            self._loop.call_soon_threadsafe(self._continue_waiting)

    def shutdown(self) -> None:
        """작업 중이면 중지·정리를 기다린 뒤 이벤트 루프를 멈춘다. 여러 번 호출해도 안전."""
        if self._closed or self._loop is None:
            return
        timeout = self.config.shutdown_timeout_sec
        if self.is_busy():
            try:
                asyncio.run_coroutine_threadsafe(self._cancel(), self._loop).result(timeout=5)
                self._job_future.result(timeout=timeout)
            except Exception as exc:
                logger.error("종료 중 작업 정리가 제한 시간(%s초) 안에 끝나지 않았거나 실패: %s",
                             timeout, exc)
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.error("실행부 스레드가 제한 시간 안에 종료되지 않았습니다")

    # ---------- 실행부 스레드 ----------
    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(
            lambda lp, ctx: logger.error("이벤트 루프 오류: %s", ctx.get("message"),
                                         exc_info=ctx.get("exception")))
        self._loop = loop
        logger.info("실행부 이벤트 루프 시작: %s", type(loop).__name__)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
                logger.info("실행부 이벤트 루프 종료")

    def _on_job_done(self, future: concurrent.futures.Future) -> None:
        try:
            summary = future.result()
        except Exception as exc:  # _run_job 내부 버그 등
            logger.exception("작업 처리 중 예상하지 못한 오류")
            summary = {"state": "failed", "error": str(exc), "sites": []}
        self.job_finished.emit(summary)
        self.busy_changed.emit(False)

    def _set_state(self, result: SiteRunResult, state: str, message: str) -> None:
        if result.state == state and result.message == message:
            return
        result.state = state
        result.message = message
        if state == "failed":
            result.error = message
        level = logging.WARNING if state in ("failed", "needs_user") else logging.INFO
        site_logger.log(level, "[%s] %s: %s", result.site_name, STATE_LABELS.get(state, state), message)
        self.site_state.emit(result.site_id, state, message)

    def _emit_waiting(self, job: _Job) -> None:
        self.waiting_changed.emit(list(job.waiting))

    def _continue_waiting(self) -> None:
        job = self._job
        if job is None or not job.waiting:
            logger.info("계속 요청: 사용자 확인을 기다리는 사이트가 없습니다")
            return
        for site_id, event in list(job.waiting.items()):
            logger.info("계속 요청 전달: %s", site_id)
            event.set()

    async def _cancel(self) -> None:
        job = self._job
        if job is None or job.cancel_event.is_set():
            return
        job.cancel_event.set()
        logger.info("중지 요청 수신: 대기 작업은 즉시 중단하고, 진행 중인 브라우저는 닫아서 중단합니다")
        for result in job.results.values():
            if result.state in ACTIVE_STATES:
                self._set_state(result, "cancelling", "중지 처리 중")
        if job.browser is not None:
            await job.browser.force_close_contexts()

    async def _run_job(self, query: str, site_ids: list[str],
                       extract_body: bool = False) -> dict[str, Any]:
        cfg = self.config
        results = {
            sid: SiteRunResult(site_id=sid, site_name=self._sites[sid].DISPLAY_NAME)
            for sid in site_ids
        }
        job = _Job(query=query, site_ids=site_ids, cancel_event=asyncio.Event(),
                   results=results, extract_body=extract_body)
        self._job = job
        logger.info("작업 시작: 검색어=%r, 사이트=%s, 동시 실행=%s, 사이트별 최대=%s건",
                    query, site_ids, cfg.max_concurrency, cfg.max_results_per_site)
        for result in results.values():
            self._set_state(result, "queued", "실행 대기")
        playwright = None
        try:
            playwright = await self._playwright_factory().start()
            job.browser = BrowserManager(playwright, cfg.profiles_dir,
                                         cfg.action_timeout_ms, cfg.navigation_timeout_ms)
            semaphore = asyncio.Semaphore(cfg.max_concurrency)
            tasks = [asyncio.create_task(self._run_site(job, semaphore, results[sid]))
                     for sid in site_ids]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:
            logger.exception("Playwright 시작 또는 작업 실행 실패")
            for result in results.values():
                if result.state in ACTIVE_STATES + ("cancelling",):
                    self._set_state(result, "failed", f"실행부 오류: {type(exc).__name__}: {exc}")
        finally:
            if job.browser is not None:
                await job.browser.close_all()
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception as exc:
                    logger.warning("Playwright 종료 중 오류: %s", exc)
            for result in results.values():
                if result.state == "cancelling":
                    self._set_state(result, "cancelled", "중지됨")
            self._job = None
            self.waiting_changed.emit([])
        return self._summary(job)

    async def _run_site(self, job: _Job, semaphore: asyncio.Semaphore,
                        result: SiteRunResult) -> None:
        module = self._sites[result.site_id]
        async with semaphore:
            if job.cancel_event.is_set():
                self._set_state(result, "cancelled", "시작 전 중지됨")
                return
            result.started_at = now_iso()
            self._set_state(result, "running", "브라우저 시작")
            ctx = SiteRunContext(self, job, result)
            page = None
            try:
                context = await job.browser.open(module.PROFILE_NAME)
                ctx.checkpoint("페이지 준비")
                page = context.pages[0] if context.pages else await context.new_page()
                result.items = await module.run(page, job.query, ctx)
                if job.extract_body:
                    for item in result.items:
                        item.body_status = item.processing["body"] = "pending"
                    # 사이트 동시 실행 수가 2여도 본문 가져오기는 작업 전체에서 항상 한 건씩.
                    async with job.body_lock:
                        await self._extract_bodies(context, result, ctx)
                message = f"{len(result.items)}건 수집"
                if job.extract_body:
                    count = sum(item.body_status == "extracted" for item in result.items)
                    message += f" / 본문 후보 {count}건, 나머지 {len(result.items) - count}건 상세 확인"
                self._set_state(result, "done", message)
            except JobCancelled:
                self._set_state(result, "cancelled", "중지 요청으로 종료")
            except ProfileInUseError as exc:
                self._set_state(result, "failed", str(exc))
            except SiteBlocked as exc:
                self._set_state(result, "failed", f"{exc} - 자동 우회 없이 중지")
            except SiteError as exc:
                self._set_state(result, "failed", str(exc))
            except PlaywrightError as exc:
                first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
                if job.cancel_event.is_set():
                    self._set_state(result, "cancelled", "중지 요청으로 브라우저를 닫아 진행 중 동작을 중단")
                else:
                    site_logger.error("[%s] 브라우저 오류 상세", result.site_name, exc_info=exc)
                    self._set_state(result, "failed", f"브라우저 오류: {first_line}")
            except Exception as exc:
                site_logger.error("[%s] 예상하지 못한 오류", result.site_name, exc_info=exc)
                self._set_state(result, "failed", f"{type(exc).__name__}: {exc}")
            finally:
                if job.cancel_event.is_set():
                    for item in result.items:
                        if item.body_status in ("pending", "processing"):
                            item.body_status = item.processing["body"] = "cancelled"
                            item.body_error = "중지 요청으로 미처리"
                if result.final_url is None and page is not None:
                    try:
                        result.final_url = page.url
                    except Exception:
                        pass
                await job.browser.close(module.PROFILE_NAME)
                result.finished_at = now_iso()
                self.site_finished.emit(result.to_dict())

    async def _extract_bodies(self, context: Any, result: SiteRunResult,
                              ctx: SiteRunContext) -> None:
        """검색 결과를 보존하면서 기사별 본문 실패를 격리한다."""
        for index, item in enumerate(result.items, 1):
            ctx.checkpoint(f"본문 추출 {index}/{len(result.items)}")
            if not valid_article_url(item.original_link):
                item.body_status = item.processing["body"] = "skipped"
                item.body_error = "확인된 원문 링크 없음; 검색 설명으로 대체하지 않음"
                continue
            item.body_status = item.processing["body"] = "processing"
            article_page = None
            try:
                article_page = await context.new_page()
                body = await extract_article(
                    article_page, item.original_link, ctx.navigation_timeout_ms
                )
                ctx.checkpoint(f"본문 처리 결과 {index}/{len(result.items)}")
                item.body_status = item.processing["body"] = body.status
                item.body_text = body.text
                item.body_error = body.error
                item.body_final_url = body.final_url
                item.body_method = body.method
                if body.status == "extracted":
                    item.body_collected_at = now_iso()
                    ctx.info(f"본문 후보 추출 {index}: {len(body.text or '')}자")
                else:
                    ctx.warning(f"본문 {index}: {body.status} / {body.error}")
            except JobCancelled:
                raise
            except Exception as exc:
                ctx.checkpoint(f"본문 오류 확인 {index}/{len(result.items)}")
                item.body_status = item.processing["body"] = "failed"
                item.body_error = f"{type(exc).__name__}: {exc}"
                ctx.warning(f"본문 {index}: {item.body_error}")
            finally:
                if article_page is not None:
                    try:
                        await article_page.close()
                    except Exception as exc:
                        ctx.warning(f"기사 탭 종료: {exc}")

    def _summary(self, job: _Job) -> dict[str, Any]:
        states = [r.state for r in job.results.values()]
        if job.cancel_event.is_set():
            overall = "cancelled"
        elif states and all(s == "done" for s in states):
            overall = "done"
        elif any(s == "done" for s in states):
            overall = "partial"
        else:
            overall = "failed"
        cfg = self.config
        return {
            "schema_version": 2,
            "query": job.query,
            "search_type": cfg.search_type,
            "state": overall,
            "started_at": job.started_at,
            "finished_at": now_iso(),
            "settings": {
                "max_results_per_site": cfg.max_results_per_site,
                "max_concurrency": cfg.max_concurrency,
                "extract_body": job.extract_body,
                "body_concurrency": 1,
                "sort": "사이트 기본 정렬(지정하지 않음)",
                "period": "지정하지 않음",
            },
            "not_implemented": ["핵심 키워드", "본문 요약", "중복 제거", "CSV 내보내기"],
            "sites": [r.to_dict() for r in job.results.values()],
        }
