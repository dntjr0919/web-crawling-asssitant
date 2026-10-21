"""config.json 로드. 파일에 없거나 잘못된 값은 기본값을 사용하고 로그로 알린다."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 현재 지원하는 검색 대상 종류
SUPPORTED_SEARCH_TYPES = ("news", "web", "blog")

logger = logging.getLogger(__name__)


@dataclass
class AppConfig:
    max_results_per_site: int = 10
    # 동시 실행 수. 1이 기본이며 2는 사용자 검증 대기 상태다.
    max_concurrency: int = 1
    search_type: str = "news"
    action_timeout_ms: int = 10_000
    navigation_timeout_ms: int = 30_000
    result_wait_timeout_ms: int = 15_000
    manual_wait_timeout_sec: int = 300
    max_manual_rounds: int = 3
    shutdown_timeout_sec: int = 30
    profiles_dir: Path = PROJECT_ROOT / "profiles"
    results_dir: Path = PROJECT_ROOT / "results"
    logs_dir: Path = PROJECT_ROOT / "logs"
    enabled_sites: dict[str, bool] = field(
        default_factory=lambda: {"naver": True, "google": True}
    )
    warnings: list[str] = field(default_factory=list)


def _int_in_range(raw: dict, key: str, default: int, low: int, high: int,
                  warnings: list[str]) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        warnings.append(f"{key}={value!r}: 정수가 아니어서 기본값 {default} 사용")
        return default
    if value < low or value > high:
        clamped = min(max(value, low), high)
        warnings.append(f"{key}={value}: 허용 범위 {low}~{high}를 벗어나 {clamped} 사용")
        return clamped
    return value


def _resolve_dir(raw: dict, key: str, default: Path) -> Path:
    value = raw.get(key)
    if not value:
        return default
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_config(path: Path = CONFIG_PATH) -> AppConfig:
    warnings: list[str] = []
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                warnings.append("config.json 최상위가 객체가 아니어서 기본값 사용")
                raw = {}
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"config.json을 읽지 못해 기본값 사용: {exc}")
            raw = {}
    else:
        warnings.append("config.json이 없어 기본값 사용")

    d = AppConfig()
    search_type = raw.get("search_type", d.search_type)
    if search_type not in SUPPORTED_SEARCH_TYPES:
        warnings.append(f"search_type={search_type!r}: 미구현이므로 'news' 사용")
        search_type = "news"

    enabled = dict(d.enabled_sites)
    raw_enabled = raw.get("enabled_sites", {})
    if isinstance(raw_enabled, dict):
        for key, value in raw_enabled.items():
            if isinstance(value, bool):
                enabled[key] = value

    cfg = AppConfig(
        max_results_per_site=_int_in_range(raw, "max_results_per_site", d.max_results_per_site, 1, 50, warnings),
        max_concurrency=_int_in_range(raw, "max_concurrency", d.max_concurrency, 1, 2, warnings),
        search_type=search_type,
        action_timeout_ms=_int_in_range(raw, "action_timeout_ms", d.action_timeout_ms, 1_000, 60_000, warnings),
        navigation_timeout_ms=_int_in_range(raw, "navigation_timeout_ms", d.navigation_timeout_ms, 5_000, 120_000, warnings),
        result_wait_timeout_ms=_int_in_range(raw, "result_wait_timeout_ms", d.result_wait_timeout_ms, 1_000, 120_000, warnings),
        manual_wait_timeout_sec=_int_in_range(raw, "manual_wait_timeout_sec", d.manual_wait_timeout_sec, 10, 3_600, warnings),
        max_manual_rounds=_int_in_range(raw, "max_manual_rounds", d.max_manual_rounds, 1, 10, warnings),
        shutdown_timeout_sec=_int_in_range(raw, "shutdown_timeout_sec", d.shutdown_timeout_sec, 5, 120, warnings),
        profiles_dir=_resolve_dir(raw, "profiles_dir", d.profiles_dir),
        results_dir=_resolve_dir(raw, "results_dir", d.results_dir),
        logs_dir=_resolve_dir(raw, "logs_dir", d.logs_dir),
        enabled_sites=enabled,
        warnings=warnings,
    )
    return cfg
