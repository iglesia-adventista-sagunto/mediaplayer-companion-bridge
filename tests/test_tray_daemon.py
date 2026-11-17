import logging
from logging.handlers import RotatingFileHandler
import os
import tempfile
import unittest

import requests

from app import create_app
from tests.test_app import FakeWallpaperBackend
from tray_daemon import (
    BridgeServerController,
    configure_logging,
    create_tray_icon_image,
    resolve_log_path,
)


class TestTrayIconAndLogging(unittest.TestCase):
    def test_create_tray_icon_image_states(self):
        icons = {}
        for state in ("ok", "warning", "error"):
            img = create_tray_icon_image(state, size=64)
            self.assertEqual(img.size, (64, 64))
            self.assertEqual(img.mode, "RGBA")
            icons[state] = img.tobytes()

        self.assertNotEqual(icons["ok"], icons["warning"])
        self.assertNotEqual(icons["ok"], icons["error"])
        self.assertNotEqual(icons["warning"], icons["error"])

    def test_resolve_log_path_beside_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, "cache")
            log_path = resolve_log_path(cache_dir=cache_dir)
            self.assertEqual(
                os.path.abspath(log_path),
                os.path.abspath(os.path.join(tmp, "logs", "bridge.log")),
            )


class TestBridgeServerController(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stills_dir = os.path.join(self.tmp.name, "stills")
        self.cache_dir = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.stills_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        configure_logging(cache_dir=self.cache_dir)
        self.backend = FakeWallpaperBackend()

    def tearDown(self):
        root_logger = logging.getLogger()
        tmp_abs = os.path.abspath(self.tmp.name)
        for handler in list(root_logger.handlers):
            if isinstance(handler, RotatingFileHandler):
                base = os.path.abspath(getattr(handler, "baseFilename", ""))
                if base.startswith(tmp_abs):
                    root_logger.removeHandler(handler)
                    handler.close()
        self.tmp.cleanup()

    def _make_config(self, port: int = 0, monitor: str = "extended"):
        def loader():
            return {
                "port": port,
                "host": "127.0.0.1",
                "monitor": monitor,
                "position": "fill",
                "stills_dir": self.stills_dir,
                "cache_dir": self.cache_dir,
                "max_cache_files": 5,
                "url_timeout_seconds": 5,
                "presets": [],
            }

        return loader

    def test_start_diagnose_restart_stop(self):
        ctrl = BridgeServerController(create_app, self._make_config(port=0), backend=self.backend, version="1.0.0")
        try:
            self.assertTrue(ctrl.start())
            diag = ctrl.get_diagnostics(check_http=True)
            self.assertEqual(diag["status"], "running")
            self.assertEqual(diag["tray_state"], "ok")
            self.assertIsNone(diag["http_error"])
            self.assertGreater(diag["port"], 0)
            self.assertEqual(len(diag["monitors"]), 2)

            resp = requests.get(f"http://127.0.0.1:{ctrl.port}/health", timeout=2.0)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["monitor_ok"])

            self.assertTrue(ctrl.restart())
            diag_after = ctrl.get_diagnostics(check_http=True)
            self.assertEqual(diag_after["status"], "running")
            self.assertEqual(diag_after["restart_count"], 1)

            text = ctrl.format_diagnostics_text(diag_after)
            self.assertIn("MediaPlayer Companion Bridge v1.0.0", text)
            self.assertIn("Detected Monitors (2):", text)
            self.assertIn("Recent Logs:", text)
        finally:
            ctrl.stop()

    def test_port_conflict_sets_error_state(self):
        ctrl1 = BridgeServerController(create_app, self._make_config(port=0), backend=self.backend, version="1.0.0")
        self.assertTrue(ctrl1.start())
        try:
            busy_port = ctrl1.port
            ctrl2 = BridgeServerController(
                create_app, self._make_config(port=busy_port), backend=self.backend, version="1.0.0"
            )
            self.assertFalse(ctrl2.start())
            diag = ctrl2.get_diagnostics()
            self.assertEqual(diag["status"], "error")
            self.assertEqual(diag["tray_state"], "error")
            self.assertIsNotNone(diag["last_start_error"])
        finally:
            ctrl1.stop()

    def test_missing_monitor_sets_warning_state(self):
        ctrl = BridgeServerController(
            create_app, self._make_config(port=0, monitor="nonexistent_monitor"), backend=self.backend, version="1.0.0"
        )
        try:
            self.assertTrue(ctrl.start())
            diag = ctrl.get_diagnostics()
            self.assertEqual(diag["status"], "running")
            self.assertEqual(diag["tray_state"], "warning")
            self.assertIsNotNone(diag["monitor_error"])
        finally:
            ctrl.stop()

    def test_ui_url_uses_config_host_and_port(self):
        def custom_loader():
            return {
                "port": 5099,
                "host": "localhost",
                "monitor": "extended",
                "position": "fill",
                "stills_dir": self.stills_dir,
                "cache_dir": self.cache_dir,
                "max_cache_files": 5,
                "url_timeout_seconds": 5,
                "presets": [],
            }

        ctrl = BridgeServerController(create_app, custom_loader, backend=self.backend, version="1.0.0")
        self.assertEqual(ctrl.ui_url, "http://localhost:5099/ui")

        def wildcard_loader():
            cfg = custom_loader()
            cfg["host"] = "0.0.0.0"
            cfg["port"] = 5077
            return cfg

        ctrl_wildcard = BridgeServerController(create_app, wildcard_loader, backend=self.backend, version="1.0.0")
        self.assertEqual(ctrl_wildcard.ui_url, "http://127.0.0.1:5077/ui")


if __name__ == "__main__":
    unittest.main()
