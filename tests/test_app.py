import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from app import create_app
from wallpaper_service import MonitorNotFoundError, POSITION_TO_CODE, resolve_monitor


def _make_png_bytes(color=(12, 34, 56), size=(64, 36)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeWallpaperBackend:
    def __init__(self, monitors=None):
        self.monitors = monitors or [
            {
                "index": 0,
                "id": r"\\?\DISPLAY#HXA0000#EXTENDED",
                "active": True,
                "primary": False,
                "left": -1920,
                "top": 0,
                "right": 0,
                "bottom": 1080,
                "width": 1920,
                "height": 1080,
                "wallpaper": "",
            },
            {
                "index": 1,
                "id": r"\\?\DISPLAY#HWP3325#PRIMARY",
                "active": True,
                "primary": True,
                "left": 0,
                "top": 0,
                "right": 1920,
                "bottom": 1080,
                "width": 1920,
                "height": 1080,
                "wallpaper": "",
            },
        ]
        self.position = "fill"
        self.calls = []

    def list_monitors(self):
        return [dict(m) for m in self.monitors]

    def get_wallpaper(self, monitor_id):
        for m in self.monitors:
            if m["id"] == monitor_id:
                return m["wallpaper"]
        return ""

    def set_wallpaper(self, monitor_id, image_path):
        self.calls.append(("set_wallpaper", monitor_id, image_path))
        for m in self.monitors:
            if m["id"] == monitor_id:
                m["wallpaper"] = image_path
                return
        raise MonitorNotFoundError(f"Unknown monitor {monitor_id}")

    def get_position(self):
        return self.position

    def set_position(self, position):
        key = position.strip().lower()
        if key not in POSITION_TO_CODE:
            raise ValueError(f"Invalid position: {position}")
        self.position = key
        self.calls.append(("set_position", key))
        return key


class TestResolveMonitor(unittest.TestCase):
    def setUp(self):
        self.backend = FakeWallpaperBackend()
        self.monitors = self.backend.list_monitors()

    def test_extended_picks_non_primary(self):
        mon = resolve_monitor(self.monitors, "extended")
        self.assertEqual(mon["index"], 0)
        self.assertFalse(mon["primary"])

    def test_primary_picks_primary(self):
        mon = resolve_monitor(self.monitors, "primary")
        self.assertEqual(mon["index"], 1)
        self.assertTrue(mon["primary"])

    def test_index_and_substring(self):
        self.assertEqual(resolve_monitor(self.monitors, "1")["index"], 1)
        self.assertEqual(resolve_monitor(self.monitors, "hxa0000")["index"], 0)

    def test_no_extended_raises(self):
        only_primary = [self.monitors[1]]
        with self.assertRaises(MonitorNotFoundError):
            resolve_monitor(only_primary, "extended")


class TestMediaPlayerBridgeAPI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stills_dir = os.path.join(self.tmp.name, "stills")
        self.cache_dir = os.path.join(self.tmp.name, "cache")
        os.makedirs(self.stills_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)

        with open(os.path.join(self.stills_dir, "logo.png"), "wb") as f:
            f.write(_make_png_bytes((255, 0, 0)))

        self.backend = FakeWallpaperBackend()
        self.config = {
            "port": 5002,
            "host": "127.0.0.1",
            "monitor": "extended",
            "position": "fill",
            "stills_dir": self.stills_dir,
            "cache_dir": self.cache_dir,
            "max_cache_files": 5,
            "url_timeout_seconds": 5,
            "presets": [
                {"name": "latest-thumbnail", "url": "http://127.0.0.1:5001/latest"},
                {"name": "brand-logo", "file": "logo.png"},
                {"name": "black", "color": "#000000"},
            ],
        }
        self.app = create_app(config=self.config, backend=self.backend)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_and_status(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["monitor_ok"])
        self.assertEqual(data["stills_count"], 1)

        res_status = self.client.get("/status")
        self.assertEqual(res_status.status_code, 200)
        self.assertEqual(res_status.get_json()["active_monitor"]["index"], 0)

    def test_apply_still_by_name_and_current_endpoints(self):
        res = self.client.get("/still/logo")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["applied"]["monitor"]["index"], 0)
        self.assertTrue(self.backend.monitors[0]["wallpaper"].endswith("logo.png"))

        self.assertEqual(self.client.get("/current/name").data.decode(), "logo")
        img_res = self.client.get("/current/image")
        self.assertEqual(img_res.status_code, 200)
        self.assertIn("no-store", img_res.headers.get("Cache-Control", ""))

    def test_apply_missing_still_returns_404(self):
        res = self.client.get("/still/does_not_exist")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(res.get_json()["ok"])

    def test_clear_black_creates_solid_png(self):
        res = self.client.post("/clear")
        self.assertEqual(res.status_code, 200)
        applied_path = res.get_json()["applied"]["path"]
        self.assertTrue(os.path.isfile(applied_path))
        with Image.open(applied_path) as img:
            self.assertEqual(img.size, (1920, 1080))
            self.assertEqual(img.getpixel((0, 0)), (0, 0, 0))

    def test_upload_save_and_apply(self):
        png_data = _make_png_bytes((0, 200, 100))
        res = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(png_data), "sermon.png"), "name": "sermon_slide"},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body["saved"])
        self.assertEqual(body["filename"], "sermon_slide.png")
        self.assertTrue(os.path.isfile(os.path.join(self.stills_dir, "sermon_slide.png")))
        self.assertEqual(self.client.get("/current/name").data.decode(), "sermon_slide")

        del_res = self.client.delete("/stills/sermon_slide.png")
        self.assertEqual(del_res.status_code, 200)
        self.assertFalse(os.path.isfile(os.path.join(self.stills_dir, "sermon_slide.png")))

    def test_upload_invalid_image_returns_400(self):
        res = self.client.post(
            "/upload",
            data={"file": (io.BytesIO(b"not an image"), "bad.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(res.status_code, 400)

    @patch("app.requests.get")
    def test_apply_url_and_preset_url(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.content = _make_png_bytes((10, 20, 30))
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        res = self.client.get("/preset/latest-thumbnail")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["applied"]["name"], "latest-thumbnail")
        mock_get.assert_called_once()

    def test_monitor_and_position_switching(self):
        res_pos = self.client.post("/position/fit")
        self.assertEqual(res_pos.status_code, 200)
        self.assertEqual(res_pos.get_json()["position"], "fit")

        res_bad_pos = self.client.post("/position/invalid_mode")
        self.assertEqual(res_bad_pos.status_code, 400)

        res_mon = self.client.post("/monitor/1")
        self.assertEqual(res_mon.status_code, 200)
        self.assertEqual(res_mon.get_json()["monitor"]["index"], 1)


if __name__ == "__main__":
    unittest.main()
