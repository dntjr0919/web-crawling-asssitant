"""앱 시작점: .venv\\Scripts\\python.exe main.py"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtWidgets import QApplication

from crawler_app.config import load_config
from crawler_app.gui import LogBridge, MainWindow, QtLogHandler
from crawler_app.runner import Runner

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(threadName)s %(name)s: %(message)s"


def setup_logging(logs_dir, bridge: LogBridge) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(logs_dir / "app.log", maxBytes=1_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)
    gui_handler = QtLogHandler(bridge)
    gui_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    root.addHandler(gui_handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(console)


def main() -> int:
    config = load_config()
    app = QApplication(sys.argv)
    bridge = LogBridge()
    setup_logging(config.logs_dir, bridge)
    log = logging.getLogger("main")

    def excepthook(exc_type, exc, tb):
        log.critical("처리되지 않은 오류", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    for warning in config.warnings:
        log.warning("설정: %s", warning)

    runner = Runner(config)
    runner.start()
    window = MainWindow(config, runner, bridge)
    window.show()
    log.info("앱 시작 (Python %s)", sys.version.split()[0])
    code = app.exec()
    runner.shutdown()  # 창 닫기에서 이미 정리했다면 아무 일도 하지 않음
    log.info("앱 종료")
    return code


if __name__ == "__main__":
    sys.exit(main())
