"""자동화 전용 프로필과 브라우저 컨텍스트 관리.

- 사이트별 전용 user_data_dir 사용 (평소 쓰는 Chrome 프로필 사용 안 함)
- 같은 프로필 동시 사용 방지: 프로세스 내부 집합 + OS 파일 잠금
- 이 앱이 연 컨텍스트만 닫는다. 다른 브라우저 프로세스는 건드리지 않는다.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ProfileInUseError(RuntimeError):
    pass


class ProfileLock:
    """프로필 폴더 옆의 .lock 파일을 OS 잠금으로 점유한다.

    프로세스가 비정상 종료돼도 OS가 잠금을 해제하므로 오래된 잠금이 남지 않는다.
    잠금 파일 자체는 삭제하지 않는다(삭제 경쟁을 피하기 위함).
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fh = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise ProfileInUseError(
                f"프로필이 다른 실행에서 사용 중입니다: {self.lock_path.stem}"
            ) from exc
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            logger.warning("프로필 잠금 해제 중 오류(파일 닫기로 해제됨): %s", self.lock_path)
        finally:
            self._fh.close()
            self._fh = None


class BrowserManager:
    """Playwright 객체를 소유한 실행부 스레드에서만 사용한다."""

    def __init__(self, playwright: Any, profiles_dir: Path,
                 action_timeout_ms: int, navigation_timeout_ms: int) -> None:
        self._pw = playwright
        self._profiles_dir = profiles_dir
        self._action_timeout_ms = action_timeout_ms
        self._navigation_timeout_ms = navigation_timeout_ms
        self._contexts: dict[str, Any] = {}
        self._locks: dict[str, ProfileLock] = {}

    def profile_dir(self, profile_name: str) -> Path:
        return self._profiles_dir / profile_name

    async def open(self, profile_name: str) -> Any:
        if profile_name in self._locks:
            raise ProfileInUseError(f"프로필이 이미 이 앱에서 사용 중입니다: {profile_name}")
        lock = ProfileLock(self._profiles_dir / f"{profile_name}.lock")
        lock.acquire()
        self._locks[profile_name] = lock
        try:
            user_data_dir = self.profile_dir(profile_name)
            user_data_dir.mkdir(parents=True, exist_ok=True)
            logger.info("전용 프로필로 브라우저 실행: %s", user_data_dir)
            context = await self._pw.chromium.launch_persistent_context(
                str(user_data_dir),
                headless=False,
                no_viewport=True,
            )
            context.set_default_timeout(self._action_timeout_ms)
            context.set_default_navigation_timeout(self._navigation_timeout_ms)
            self._contexts[profile_name] = context
            return context
        except BaseException:
            self._locks.pop(profile_name).release()
            raise

    async def _close_context(self, profile_name: str) -> None:
        context = self._contexts.pop(profile_name, None)
        if context is None:
            return
        try:
            await context.close()
            logger.info("브라우저 종료: %s", profile_name)
        except Exception as exc:  # 이미 닫힌 경우 포함
            logger.warning("브라우저 종료 중 오류(%s): %s", profile_name, exc)

    async def close(self, profile_name: str) -> None:
        """사이트 작업(소유자)이 끝날 때 호출. 컨텍스트를 닫고 잠금을 해제한다."""
        try:
            await self._close_context(profile_name)
        finally:
            lock = self._locks.pop(profile_name, None)
            if lock is not None:
                lock.release()

    async def force_close_contexts(self) -> None:
        """중지 요청 시 호출. 진행 중인 Playwright 호출은 오류로 끝나게 된다.

        잠금은 각 사이트 작업이 정리 단계에서 해제한다.
        """
        for name in list(self._contexts):
            await self._close_context(name)

    async def close_all(self) -> None:
        """작업 전체가 끝난 뒤 남은 컨텍스트와 잠금을 정리한다."""
        for name in list(self._contexts):
            await self._close_context(name)
        for name in list(self._locks):
            self._locks.pop(name).release()
