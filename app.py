import argparse
import hashlib
import io
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request, send_file, send_from_directory
from PIL import Image, ImageColor

from wallpaper_service import (
    POSITION_TO_CODE,
    MonitorNotFoundError,
    WallpaperError,
    WindowsDesktopWallpaperBackend,
    resolve_monitor,
)

if sys.stderr is not None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
    )
logger = logging.getLogger("mediaplayer-bridge")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATE_CONFIG_PATH = os.path.join(BASE_DIR, "config.template.json")
VERSION = "1.0.0"

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tiff", ".tif", ".webp")


def _resolve_dir(path_val: str | None, default_rel: str) -> str:
    raw = path_val or default_rel
    if os.path.isabs(raw):
        return raw
    return os.path.abspath(os.path.join(BASE_DIR, raw))


def _parse_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _sanitize_filename(name: str) -> str:
    base = os.path.basename(str(name or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    return cleaned.strip("._") or "still"


def load_config() -> dict:
    """Load configuration from CONFIG_PATH (or config.json / template) with env overrides."""
    config_path = os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    data = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded configuration from %s", config_path)
    elif os.path.exists(TEMPLATE_CONFIG_PATH):
        with open(TEMPLATE_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.warning("config.json not found at %s; using config.template.json defaults", config_path)
    else:
        logger.warning("No config.json or template found; using built-in defaults")

    return {
        "port": int(os.environ.get("PORT") or data.get("port", 5002)),
        "host": os.environ.get("HOST") or data.get("host", "0.0.0.0"),
        "monitor": os.environ.get("MONITOR") or data.get("monitor", "extended"),
        "position": (os.environ.get("POSITION") or data.get("position", "fill")).strip().lower(),
        "stills_dir": _resolve_dir(os.environ.get("STILLS_DIR") or data.get("stills_dir"), "stills"),
        "cache_dir": _resolve_dir(os.environ.get("CACHE_DIR") or data.get("cache_dir"), "cache"),
        "max_cache_files": int(os.environ.get("MAX_CACHE_FILES") or data.get("max_cache_files", 20)),
        "url_timeout_seconds": int(
            os.environ.get("URL_TIMEOUT_SECONDS") or data.get("url_timeout_seconds", 15)
        ),
        "presets": data.get("presets") if isinstance(data.get("presets"), list) else [],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prune_cache(cache_dir: str, max_files: int, keep_path: str | None = None) -> None:
    if max_files <= 0 or not os.path.isdir(cache_dir):
        return
    entries = []
    for fname in os.listdir(cache_dir):
        fpath = os.path.join(cache_dir, fname)
        if os.path.isfile(fpath):
            entries.append((os.path.getmtime(fpath), fpath))
    entries.sort(key=lambda item: item[0], reverse=True)
    for _, fpath in entries[max_files:]:
        if keep_path and os.path.abspath(fpath) == os.path.abspath(keep_path):
            continue
        try:
            os.remove(fpath)
        except OSError:
            pass


def _prepare_png_bytes(raw_bytes: bytes) -> bytes:
    """Validate image bytes with Pillow and normalize to PNG for reliable Windows wallpaper loading."""
    with Image.open(io.BytesIO(raw_bytes)) as img:
        mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
        converted = img.convert(mode)
        out = io.BytesIO()
        converted.save(out, format="PNG")
        return out.getvalue()


def _create_solid_png_bytes(color_str: str, width: int = 1920, height: int = 1080) -> bytes:
    rgb = ImageColor.getrgb(color_str)
    img = Image.new("RGB", (max(1, width), max(1, height)), rgb[:3])
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _list_stills(stills_dir: str) -> list[dict]:
    if not os.path.isdir(stills_dir):
        return []
    items = []
    for fname in sorted(os.listdir(stills_dir), key=str.lower):
        fpath = os.path.join(stills_dir, fname)
        if not os.path.isfile(fpath):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        stat = os.stat(fpath)
        items.append(
            {
                "filename": fname,
                "name": os.path.splitext(fname)[0],
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return items


def _find_still_file(stills_dir: str, name: str) -> str | None:
    if not os.path.isdir(stills_dir):
        return None
    raw = os.path.basename(str(name or "").strip())
    if not raw:
        return None
    lower = raw.lower()
    files = [
        f
        for f in os.listdir(stills_dir)
        if os.path.isfile(os.path.join(stills_dir, f))
        and os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
    ]
    for f in files:
        if f.lower() == lower:
            return os.path.join(stills_dir, f)
    for f in files:
        if os.path.splitext(f)[0].lower() == lower:
            return os.path.join(stills_dir, f)
    return None


def _fetch_url_bytes(url: str, timeout: int) -> bytes:
    """Fetch URL bytes via requests, falling back to OS trust store on SSLError."""
    clean_url = url.strip()
    try:
        resp = requests.get(clean_url, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    except requests.exceptions.SSLError:
        req = urllib.request.Request(clean_url, headers={"User-Agent": "MediaPlayerCompanionBridge/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
                return resp.read()
        except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
            raise requests.exceptions.SSLError(str(exc)) from exc


def create_app(config: dict | None = None, backend=None) -> Flask:
    cfg = config if config is not None else load_config()
    os.makedirs(cfg["stills_dir"], exist_ok=True)
    os.makedirs(cfg["cache_dir"], exist_ok=True)

    if backend is None:
        backend = WindowsDesktopWallpaperBackend()

    app = Flask(
        __name__,
        template_folder=os.path.join(BASE_DIR, "templates"),
        static_folder=os.path.join(BASE_DIR, "static"),
    )
    app.config["BRIDGE_CONFIG"] = cfg
    app.config["WALLPAPER_BACKEND"] = backend

    state_lock = threading.Lock()
    state = {
        "target_monitor": cfg["monitor"],
        "position": cfg["position"],
        "current_still": None,
        "last_error": None,
        "total_updates": 0,
    }

    def _get_param(key: str, default=None):
        if key in request.args:
            return request.args.get(key)
        data = request.get_json(silent=True)
        if isinstance(data, dict) and key in data:
            return data.get(key)
        if key in request.form:
            return request.form.get(key)
        return default

    def _resolve_target_monitor(override=None):
        monitors = backend.list_monitors()
        with state_lock:
            selector = override if override not in (None, "") else state["target_monitor"]
        monitor = resolve_monitor(monitors, selector)
        return monitor, monitors

    def _apply_wallpaper_file(
        image_path: str,
        still_name: str,
        source: str,
        monitor_override=None,
        position_override=None,
    ) -> dict:
        monitor, _ = _resolve_target_monitor(monitor_override)
        with state_lock:
            pos = position_override or state["position"]
        if pos:
            backend.set_position(pos)
        backend.set_wallpaper(monitor["id"], image_path)

        entry = {
            "name": still_name,
            "source": source,
            "path": os.path.abspath(image_path),
            "position": pos,
            "monitor": {
                "index": monitor["index"],
                "id": monitor["id"],
                "width": monitor["width"],
                "height": monitor["height"],
                "primary": monitor["primary"],
            },
            "updated_at": _now_iso(),
        }
        with state_lock:
            state["current_still"] = entry
            state["last_error"] = None
            state["total_updates"] += 1
            if pos:
                state["position"] = pos
        logger.info("Applied still '%s' (%s) to monitor [%s] %s", still_name, source, monitor["index"], monitor["id"])
        return entry

    def _write_cache_png(png_bytes: bytes, prefix: str) -> str:
        digest = hashlib.sha256(png_bytes).hexdigest()[:8]
        fname = f"{_sanitize_filename(prefix)}_{int(time.time() * 1000)}_{digest}.png"
        fpath = os.path.join(cfg["cache_dir"], fname)
        with open(fpath, "wb") as f:
            f.write(png_bytes)
        _prune_cache(cfg["cache_dir"], cfg["max_cache_files"], keep_path=fpath)
        return fpath

    def _apply_from_url(url: str, label: str | None = None, monitor_override=None, position_override=None) -> dict:
        if not url or not str(url).strip().lower().startswith(("http://", "https://")):
            raise ValueError("A valid http:// or https:// URL is required")
        raw_bytes = _fetch_url_bytes(url, cfg["url_timeout_seconds"])
        png_bytes = _prepare_png_bytes(raw_bytes)
        cached_path = _write_cache_png(png_bytes, label or "url")
        return _apply_wallpaper_file(
            cached_path,
            still_name=label or url.strip(),
            source=f"url:{url.strip()}",
            monitor_override=monitor_override,
            position_override=position_override,
        )

    def _apply_preset(preset: dict, monitor_override=None, position_override=None) -> dict:
        pname = preset.get("name", "preset")
        if preset.get("url"):
            return _apply_from_url(
                preset["url"],
                label=pname,
                monitor_override=monitor_override,
                position_override=position_override,
            )
        if preset.get("file"):
            fpath = _find_still_file(cfg["stills_dir"], preset["file"])
            if not fpath:
                candidate = _resolve_dir(preset["file"], cfg["stills_dir"])
                if os.path.isfile(candidate):
                    fpath = candidate
            if not fpath:
                raise FileNotFoundError(f"Preset '{pname}' file not found: {preset['file']}")
            return _apply_wallpaper_file(
                fpath,
                still_name=pname,
                source=f"preset:{pname}",
                monitor_override=monitor_override,
                position_override=position_override,
            )
        if preset.get("color"):
            monitor, _ = _resolve_target_monitor(monitor_override)
            png_bytes = _create_solid_png_bytes(
                preset["color"],
                width=monitor.get("width") or 1920,
                height=monitor.get("height") or 1080,
            )
            cached_path = _write_cache_png(png_bytes, f"color_{pname}")
            return _apply_wallpaper_file(
                cached_path,
                still_name=pname,
                source=f"color:{preset['color']}",
                monitor_override=monitor_override,
                position_override=position_override,
            )
        raise ValueError(f"Preset '{pname}' must define 'url', 'file', or 'color'")

    def _error_response(exc: Exception, status_code: int = 400):
        with state_lock:
            state["last_error"] = str(exc)
        logger.warning("Request error (%s): %s", status_code, exc)
        return jsonify({"ok": False, "error": str(exc)}), status_code

    @app.route("/", methods=["GET"])
    @app.route("/ui", methods=["GET"])
    def ui_index():
        return render_template("index.html", version=VERSION, hostname=socket.gethostname())

    @app.route("/health", methods=["GET"])
    def health():
        monitors = []
        active_monitor = None
        monitor_error = None
        try:
            active_monitor, monitors = _resolve_target_monitor()
        except Exception as exc:
            monitor_error = str(exc)
            try:
                monitors = backend.list_monitors()
            except Exception:
                monitors = []
        with state_lock:
            pos = state["position"]
            target = state["target_monitor"]
        return jsonify(
            {
                "status": "ok",
                "version": VERSION,
                "hostname": socket.gethostname(),
                "target_monitor": target,
                "active_monitor": active_monitor,
                "monitor_ok": active_monitor is not None,
                "monitor_error": monitor_error,
                "monitors_count": len(monitors),
                "position": pos,
                "stills_count": len(_list_stills(cfg["stills_dir"])),
            }
        )

    @app.route("/status", methods=["GET"])
    def status():
        monitors = []
        active_monitor = None
        monitor_error = None
        live_position = None
        try:
            monitors = backend.list_monitors()
            live_position = backend.get_position()
            with state_lock:
                target = state["target_monitor"]
            active_monitor = resolve_monitor(monitors, target)
        except Exception as exc:
            monitor_error = str(exc)

        with state_lock:
            snap = dict(state)

        return jsonify(
            {
                "ok": True,
                "version": VERSION,
                "hostname": socket.gethostname(),
                "target_monitor": snap["target_monitor"],
                "active_monitor": active_monitor,
                "monitor_error": monitor_error,
                "monitors": monitors,
                "position": live_position or snap["position"],
                "current_still": snap["current_still"],
                "last_error": snap["last_error"],
                "total_updates": snap["total_updates"],
                "stills": _list_stills(cfg["stills_dir"]),
                "presets": cfg["presets"],
            }
        )

    @app.route("/current", methods=["GET"])
    def current():
        with state_lock:
            cur = state["current_still"]
        if cur:
            return jsonify({"ok": True, "current": cur})
        try:
            mon, _ = _resolve_target_monitor()
            return jsonify(
                {
                    "ok": True,
                    "current": {
                        "name": os.path.basename(mon.get("wallpaper") or "") or "unknown",
                        "source": "windows",
                        "path": mon.get("wallpaper") or "",
                        "monitor": mon,
                    },
                }
            )
        except Exception as exc:
            return _error_response(exc, 404)

    @app.route("/current/name", methods=["GET"])
    def current_name():
        with state_lock:
            cur = state["current_still"]
        if cur and cur.get("name"):
            return cur["name"], 200, {"Content-Type": "text/plain; charset=utf-8"}
        return "none", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.route("/current/image", methods=["GET"])
    def current_image():
        path = None
        with state_lock:
            cur = state["current_still"]
            if cur:
                path = cur.get("path")
        if not path or not os.path.isfile(path):
            try:
                mon, _ = _resolve_target_monitor()
                path = mon.get("wallpaper")
            except Exception:
                path = None
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "No current still image available"}), 404
        resp = send_file(path)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    @app.route("/monitors", methods=["GET"])
    def list_monitors():
        try:
            monitors = backend.list_monitors()
            with state_lock:
                target = state["target_monitor"]
            resolved_id = None
            try:
                resolved_id = resolve_monitor(monitors, target)["id"]
            except MonitorNotFoundError:
                pass
            return jsonify(
                {
                    "ok": True,
                    "target_monitor": target,
                    "resolved_monitor_id": resolved_id,
                    "monitors": monitors,
                }
            )
        except Exception as exc:
            return _error_response(exc, 500)

    @app.route("/monitor", methods=["GET", "POST"])
    @app.route("/monitor/<path:target>", methods=["GET", "POST"])
    def set_monitor(target=None):
        val = target if target is not None else _get_param("monitor")
        if val is None or str(val).strip() == "":
            with state_lock:
                return jsonify({"ok": True, "target_monitor": state["target_monitor"]})
        try:
            monitors = backend.list_monitors()
            matched = resolve_monitor(monitors, val)
            with state_lock:
                state["target_monitor"] = str(val).strip()
            return jsonify({"ok": True, "target_monitor": state["target_monitor"], "monitor": matched})
        except MonitorNotFoundError as exc:
            return _error_response(exc, 404)
        except Exception as exc:
            return _error_response(exc, 500)

    @app.route("/position", methods=["GET", "POST"])
    @app.route("/position/<mode>", methods=["GET", "POST"])
    def set_position(mode=None):
        val = mode if mode is not None else _get_param("position")
        if val is None or str(val).strip() == "":
            try:
                return jsonify(
                    {
                        "ok": True,
                        "position": backend.get_position(),
                        "valid_positions": list(POSITION_TO_CODE.keys()),
                    }
                )
            except Exception as exc:
                return _error_response(exc, 500)
        try:
            applied = backend.set_position(str(val))
            with state_lock:
                state["position"] = applied
            return jsonify({"ok": True, "position": applied})
        except ValueError as exc:
            return _error_response(exc, 400)
        except Exception as exc:
            return _error_response(exc, 500)

    @app.route("/stills", methods=["GET"])
    def list_stills():
        return jsonify(
            {
                "ok": True,
                "stills_dir": cfg["stills_dir"],
                "stills": _list_stills(cfg["stills_dir"]),
                "presets": cfg["presets"],
            }
        )

    @app.route("/stills/<path:filename>", methods=["GET"])
    def serve_still(filename):
        safe = os.path.basename(filename)
        fpath = os.path.join(cfg["stills_dir"], safe)
        if not os.path.isfile(fpath):
            return jsonify({"ok": False, "error": f"Still '{safe}' not found"}), 404
        resp = send_from_directory(cfg["stills_dir"], safe)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/stills/<path:filename>", methods=["DELETE"])
    def delete_still(filename):
        safe = os.path.basename(filename)
        fpath = os.path.join(cfg["stills_dir"], safe)
        if not os.path.isfile(fpath):
            return jsonify({"ok": False, "error": f"Still '{safe}' not found"}), 404
        os.remove(fpath)
        return jsonify({"ok": True, "deleted": safe})

    @app.route("/still/<path:name>", methods=["GET", "POST"])
    def apply_still(name):
        monitor_override = _get_param("monitor")
        position_override = _get_param("position")
        fpath = _find_still_file(cfg["stills_dir"], name)
        if fpath:
            try:
                entry = _apply_wallpaper_file(
                    fpath,
                    still_name=os.path.splitext(os.path.basename(fpath))[0],
                    source=f"still:{os.path.basename(fpath)}",
                    monitor_override=monitor_override,
                    position_override=position_override,
                )
                return jsonify({"ok": True, "applied": entry})
            except MonitorNotFoundError as exc:
                return _error_response(exc, 404)
            except (WallpaperError, OSError, ValueError) as exc:
                return _error_response(exc, 500)

        for preset in cfg["presets"]:
            if str(preset.get("name", "")).lower() == str(name).strip().lower():
                try:
                    entry = _apply_preset(preset, monitor_override, position_override)
                    return jsonify({"ok": True, "applied": entry})
                except MonitorNotFoundError as exc:
                    return _error_response(exc, 404)
                except Exception as exc:
                    return _error_response(exc, 500)

        return _error_response(FileNotFoundError(f"Still or preset '{name}' not found"), 404)

    @app.route("/preset/<name>", methods=["GET", "POST"])
    def apply_preset(name):
        monitor_override = _get_param("monitor")
        position_override = _get_param("position")
        for preset in cfg["presets"]:
            if str(preset.get("name", "")).lower() == str(name).strip().lower():
                try:
                    entry = _apply_preset(preset, monitor_override, position_override)
                    return jsonify({"ok": True, "applied": entry})
                except MonitorNotFoundError as exc:
                    return _error_response(exc, 404)
                except Exception as exc:
                    return _error_response(exc, 500)
        return _error_response(KeyError(f"Preset '{name}' not found"), 404)

    @app.route("/url", methods=["GET", "POST"])
    def apply_url():
        url = _get_param("url")
        label = _get_param("name")
        monitor_override = _get_param("monitor")
        position_override = _get_param("position")
        try:
            entry = _apply_from_url(url, label=label, monitor_override=monitor_override, position_override=position_override)
            return jsonify({"ok": True, "applied": entry})
        except ValueError as exc:
            return _error_response(exc, 400)
        except MonitorNotFoundError as exc:
            return _error_response(exc, 404)
        except requests.RequestException as exc:
            return _error_response(RuntimeError(f"Failed to fetch image URL: {exc}"), 502)
        except Exception as exc:
            return _error_response(exc, 500)

    @app.route("/upload", methods=["POST"])
    def upload_still():
        save = _parse_bool(_get_param("save"), default=True)
        apply_now = _parse_bool(_get_param("apply"), default=True)
        custom_name = _get_param("name")
        monitor_override = _get_param("monitor")
        position_override = _get_param("position")

        raw_bytes = None
        orig_filename = None
        if "file" in request.files:
            file_storage = request.files["file"]
            orig_filename = file_storage.filename
            raw_bytes = file_storage.read()
        elif request.data:
            raw_bytes = request.data

        if not raw_bytes:
            return _error_response(ValueError("No image data provided in 'file' or request body"), 400)

        try:
            png_bytes = _prepare_png_bytes(raw_bytes)
        except Exception as exc:
            return _error_response(ValueError(f"Invalid image file: {exc}"), 400)

        base_name = _sanitize_filename(
            custom_name or os.path.splitext(orig_filename or "uploaded_still")[0]
        )
        if base_name.lower().endswith(".png"):
            base_name = base_name[:-4]

        saved_path = None
        if save:
            saved_path = os.path.join(cfg["stills_dir"], f"{base_name}.png")
            with open(saved_path, "wb") as f:
                f.write(png_bytes)

        applied_entry = None
        if apply_now:
            try:
                apply_path = _write_cache_png(png_bytes, base_name)
                applied_entry = _apply_wallpaper_file(
                    apply_path,
                    still_name=base_name,
                    source=f"upload:{base_name}.png" if save else "upload:ephemeral",
                    monitor_override=monitor_override,
                    position_override=position_override,
                )
            except MonitorNotFoundError as exc:
                return _error_response(exc, 404)
            except Exception as exc:
                return _error_response(exc, 500)

        return jsonify(
            {
                "ok": True,
                "saved": bool(save),
                "filename": f"{base_name}.png" if save else None,
                "applied": applied_entry,
            }
        )

    @app.route("/clear", methods=["GET", "POST"])
    @app.route("/black", methods=["GET", "POST"])
    def clear_black():
        color = _get_param("color", "#000000")
        monitor_override = _get_param("monitor")
        position_override = _get_param("position")
        try:
            monitor, _ = _resolve_target_monitor(monitor_override)
            png_bytes = _create_solid_png_bytes(
                color,
                width=monitor.get("width") or 1920,
                height=monitor.get("height") or 1080,
            )
            cached_path = _write_cache_png(png_bytes, "black")
            entry = _apply_wallpaper_file(
                cached_path,
                still_name=f"solid:{color}",
                source=f"color:{color}",
                monitor_override=monitor_override,
                position_override=position_override,
            )
            return jsonify({"ok": True, "applied": entry})
        except ValueError as exc:
            return _error_response(exc, 400)
        except MonitorNotFoundError as exc:
            return _error_response(exc, 404)
        except Exception as exc:
            return _error_response(exc, 500)

    def _get_bridge_snapshot() -> dict:
        monitors = []
        active_monitor = None
        monitor_error = None
        try:
            active_monitor, monitors = _resolve_target_monitor()
        except Exception as exc:
            monitor_error = str(exc)
            try:
                monitors = backend.list_monitors()
            except Exception:
                monitors = []
        with state_lock:
            snap = dict(state)
        return {
            "target_monitor": snap["target_monitor"],
            "active_monitor": active_monitor,
            "monitor_error": monitor_error,
            "monitors": monitors,
            "position": snap["position"],
            "current_still": snap["current_still"],
            "last_error": snap["last_error"],
            "total_updates": snap["total_updates"],
        }

    app.extensions["get_bridge_snapshot"] = _get_bridge_snapshot

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MediaPlayer Companion Bridge")
    parser.add_argument("--no-tray", action="store_true", help="Run headless without system tray icon")
    parser.add_argument("--cache-dir", help="Override cache directory path")
    args = parser.parse_args()

    if args.cache_dir:
        os.environ["CACHE_DIR"] = args.cache_dir

    from tray_daemon import BridgeServerController, TrayDaemon, configure_logging, pystray

    use_tray = not args.no_tray and not _parse_bool(os.environ.get("NO_TRAY"), False) and pystray is not None
    if use_tray:
        controller = BridgeServerController(create_app, load_config, version=VERSION)
        TrayDaemon(controller).run()
    else:
        configuration = load_config()
        configure_logging(cache_dir=configuration.get("cache_dir"), base_dir=BASE_DIR)
        application = create_app(configuration)
        logger.info(
            "Starting MediaPlayer Companion Bridge v%s on %s:%s (monitor=%s, tray=off)",
            VERSION,
            configuration["host"],
            configuration["port"],
            configuration["monitor"],
        )
        application.run(host=configuration["host"], port=configuration["port"], threaded=True)
