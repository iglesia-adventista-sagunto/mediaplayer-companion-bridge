import collections
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone

from PIL import Image, ImageDraw
import requests
from werkzeug.serving import make_server

from wallpaper_service import WindowsDesktopWallpaperBackend, resolve_monitor

try:
    import pystray
except ImportError:  # pragma: no cover - optional in headless test envs
    pystray = None

try:
    import tkinter as tk
    from tkinter import scrolledtext, ttk
except ImportError:  # pragma: no cover
    tk = None
    scrolledtext = None
    ttk = None

logger = logging.getLogger("mediaplayer-bridge")

STATE_COLORS = {
    "ok": "#22C55E",       # Green: server running + monitor OK
    "warning": "#F59E0B",  # Amber: server running, monitor missing/error
    "error": "#EF4444",    # Red: server failed to start / stopped
}


class RingBufferHandler(logging.Handler):
    """In-memory ring buffer of formatted log lines for live diagnostics."""

    def __init__(self, capacity: int = 300):
        super().__init__()
        self._buffer: collections.deque[str] = collections.deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            msg = record.getMessage()
        with self._lock:
            self._buffer.append(msg)

    def get_lines(self) -> list[str]:
        with self._lock:
            return list(self._buffer)


_ring_handler: RingBufferHandler | None = None
_log_file_path: str | None = None


def resolve_log_path(cache_dir: str | None = None, base_dir: str | None = None) -> str:
    """Resolve a machine-local log file path so NAS-shared installs don't collide."""
    log_dir = os.environ.get("LOG_DIR")
    if not log_dir:
        env_cache = os.environ.get("CACHE_DIR") or cache_dir
        if env_cache:
            abs_cache = os.path.abspath(env_cache)
            # If cache_dir is under %LOCALAPPDATA%\...\cache, place logs beside it.
            if os.path.basename(abs_cache).lower() == "cache":
                log_dir = os.path.join(os.path.dirname(abs_cache), "logs")
            else:
                log_dir = os.path.join(abs_cache, "logs")
        else:
            root = base_dir or os.path.dirname(os.path.abspath(__file__))
            log_dir = os.path.join(root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "bridge.log")


def configure_logging(cache_dir: str | None = None, base_dir: str | None = None) -> tuple[str, RingBufferHandler]:
    """Configure rotating file logging, in-memory ring buffer, and safe console logging."""
    global _ring_handler, _log_file_path

    log_path = resolve_log_path(cache_dir=cache_dir, base_dir=base_dir)
    _log_file_path = log_path
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Avoid duplicate handlers if configure_logging() is called more than once.
    if _ring_handler is None:
        _ring_handler = RingBufferHandler(capacity=300)
        _ring_handler.setFormatter(formatter)
        root_logger.addHandler(_ring_handler)

    has_file_handler = any(
        isinstance(h, RotatingFileHandler) and os.path.abspath(getattr(h, "baseFilename", "")) == os.path.abspath(log_path)
        for h in root_logger.handlers
    )
    if not has_file_handler:
        file_handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Under pythonw.exe sys.stderr is None; only attach StreamHandler when stderr exists.
    if sys.stderr is not None and not any(
        type(h) is logging.StreamHandler for h in root_logger.handlers
    ):
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    return log_path, _ring_handler


def get_recent_logs() -> list[str]:
    if _ring_handler is None:
        return []
    return _ring_handler.get_lines()


def get_log_file_path() -> str | None:
    return _log_file_path


def create_tray_icon_image(status: str = "ok", size: int = 64) -> Image.Image:
    """Draw a recognizable MediaPlayer / ATEM monitor badge with a status dot."""
    badge_color = STATE_COLORS.get(status, STATE_COLORS["error"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer monitor bezel (dark navy with sky-blue accent border)
    pad = max(2, size // 16)
    radius = max(4, size // 6)
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad - size // 10],
        radius=radius,
        fill="#141B2D",
        outline="#38BDF8",
        width=max(2, size // 16),
    )

    # Monitor stand base
    stand_y = size - pad - size // 12
    draw.rectangle(
        [size * 0.34, stand_y, size * 0.66, size - pad],
        fill="#38BDF8",
    )

    # Play / media still triangle inside the screen
    cx, cy = size * 0.48, (size - size // 10) * 0.50
    tri_r = size * 0.19
    triangle = [
        (cx - tri_r * 0.65, cy - tri_r),
        (cx - tri_r * 0.65, cy + tri_r),
        (cx + tri_r * 1.05, cy),
    ]
    draw.polygon(triangle, fill="#F8FAFC")

    # Status dot in bottom-right corner
    dot_r = max(5, int(size * 0.17))
    dot_cx = size - dot_r - 1
    dot_cy = size - dot_r - 1
    draw.ellipse(
        [dot_cx - dot_r - 2, dot_cy - dot_r - 2, dot_cx + dot_r + 2, dot_cy + dot_r + 2],
        fill="#0F172A",
    )
    draw.ellipse(
        [dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r],
        fill=badge_color,
    )

    return img


def _open_path_in_os(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _resolve_connect_host(host: str | None) -> str:
    """Map wildcard bind addresses (e.g. 0.0.0.0) to 127.0.0.1 for local client connections."""
    cleaned = (host or "").strip()
    if not cleaned or cleaned in ("0.0.0.0", "::", "[::]"):
        return "127.0.0.1"
    return cleaned


def _build_ui_url(host: str | None, port: int) -> str:
    return f"http://{_resolve_connect_host(host)}:{port}/ui"


def _get_local_ips() -> list[str]:
    ips = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


class BridgeServerController:
    """Manages the Werkzeug HTTP server lifecycle (start, stop, restart, diagnose)."""

    def __init__(self, app_factory, config_loader, backend=None, version: str = "1.0.0"):
        self._app_factory = app_factory
        self._config_loader = config_loader
        self._backend = backend
        self._version = version

        self._lock = threading.Lock()
        self._server = None
        self._flask_app = None
        self._thread: threading.Thread | None = None
        self._config: dict = {}
        self._status = "stopped"
        self._last_start_error: str | None = None
        self._started_at: float | None = None
        self._started_at_iso: str | None = None
        self._restart_count = 0
        self._bound_port: int | None = None

    def start(self) -> bool:
        with self._lock:
            if self._server is not None:
                return True
            self._status = "starting"
            self._last_start_error = None

        try:
            cfg = self._config_loader()
            configure_logging(cache_dir=cfg.get("cache_dir"))
            if cfg.get("port"):
                probe_host = _resolve_connect_host(cfg.get("host"))
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.3)
                    if probe.connect_ex((probe_host, int(cfg["port"]))) == 0:
                        raise OSError(f"Port {cfg['port']} is already in use")
            flask_app = self._app_factory(cfg, backend=self._backend)
            server = make_server(cfg["host"], cfg["port"], flask_app, threaded=True)
            bound_port = server.server_port
        except Exception as exc:
            logger.exception("Failed to start MediaPlayer Companion Bridge HTTP server: %s", exc)
            with self._lock:
                self._config = locals().get("cfg", self._config)
                self._status = "error"
                self._last_start_error = str(exc)
                self._server = None
                self._thread = None
                self._bound_port = None
            return False

        thread = threading.Thread(
            target=self._serve_loop,
            args=(server,),
            name="MediaPlayerBridgeHTTP",
            daemon=True,
        )
        with self._lock:
            self._config = cfg
            self._server = server
            self._flask_app = flask_app
            self._thread = thread
            self._bound_port = bound_port
            self._status = "running"
            self._started_at = time.monotonic()
            self._started_at_iso = datetime.now(timezone.utc).isoformat()

        thread.start()
        logger.info(
            "Started MediaPlayer Companion Bridge v%s on %s:%s (monitor=%s)",
            self._version,
            cfg["host"],
            bound_port,
            cfg["monitor"],
        )
        return True

    def _serve_loop(self, server) -> None:
        try:
            server.serve_forever()
        except Exception as exc:
            logger.exception("HTTP server loop exited with error: %s", exc)
            with self._lock:
                self._status = "error"
                self._last_start_error = str(exc)

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._flask_app = None
            self._thread = None
            self._status = "stopped"

        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        logger.info("Stopped MediaPlayer Companion Bridge HTTP server")

    def restart(self) -> bool:
        logger.info("Restarting MediaPlayer Companion Bridge HTTP server...")
        self.stop()
        with self._lock:
            self._restart_count += 1
        return self.start()

    def _active_or_loaded_config(self) -> dict:
        with self._lock:
            if self._bound_port and self._config:
                return dict(self._config)
            cached = dict(self._config)
        try:
            loaded = self._config_loader()
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
        return cached

    @property
    def port(self) -> int:
        with self._lock:
            if self._bound_port:
                return self._bound_port
        cfg = self._active_or_loaded_config()
        return int(cfg.get("port", 5002))

    @property
    def ui_url(self) -> str:
        with self._lock:
            bound_port = self._bound_port
            active_cfg = dict(self._config)
        if bound_port:
            return _build_ui_url(active_cfg.get("host"), bound_port)
        cfg = self._active_or_loaded_config()
        return _build_ui_url(cfg.get("host"), int(cfg.get("port", 5002)))

    @property
    def config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def get_diagnostics(self, check_http: bool = False) -> dict:
        with self._lock:
            status = self._status
            last_start_error = self._last_start_error
            cfg = dict(self._config)
            flask_app = self._flask_app
            port = self._bound_port or int(cfg.get("port", 5002))
            started_at = self._started_at
            started_at_iso = self._started_at_iso
            restart_count = self._restart_count

        uptime_seconds = round(time.monotonic() - started_at, 1) if (started_at and status == "running") else 0.0
        snapshot = None
        http_error = None

        if flask_app is not None and "get_bridge_snapshot" in flask_app.extensions:
            try:
                snapshot = flask_app.extensions["get_bridge_snapshot"]()
            except Exception as exc:
                http_error = str(exc)

        if check_http and status == "running":
            try:
                connect_host = _resolve_connect_host(cfg.get("host"))
                r = requests.get(f"http://{connect_host}:{port}/health", timeout=1.5)
                if not r.ok:
                    http_error = f"HTTP {r.status_code}"
            except Exception as exc:
                http_error = str(exc)

        monitors = []
        active_monitor = None
        monitor_error = None
        if snapshot is not None:
            monitors = snapshot.get("monitors") or []
            active_monitor = snapshot.get("active_monitor")
            monitor_error = snapshot.get("monitor_error")
        else:
            # Fallback to direct backend inspection if server is down
            try:
                backend = self._backend or WindowsDesktopWallpaperBackend()
                monitors = backend.list_monitors()
                active_monitor = resolve_monitor(monitors, cfg.get("monitor", "extended"))
            except Exception as exc:
                monitor_error = str(exc)

        if status != "running":
            tray_state = "error"
        elif monitor_error or http_error:
            tray_state = "warning"
        else:
            tray_state = "ok"

        return {
            "version": self._version,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "status": status,
            "tray_state": tray_state,
            "port": port,
            "host": cfg.get("host", "0.0.0.0"),
            "ui_url": _build_ui_url(cfg.get("host"), port),
            "lan_urls": [f"http://{ip}:{port}/ui" for ip in _get_local_ips()],
            "started_at": started_at_iso,
            "uptime_seconds": uptime_seconds,
            "restart_count": restart_count,
            "last_start_error": last_start_error,
            "http_error": http_error,
            "target_monitor": cfg.get("monitor", "extended"),
            "active_monitor": active_monitor,
            "monitor_error": monitor_error,
            "monitors": monitors,
            "current_still": (snapshot or {}).get("current_still"),
            "last_error": (snapshot or {}).get("last_error") or last_start_error,
            "stills_dir": cfg.get("stills_dir"),
            "cache_dir": cfg.get("cache_dir"),
            "config_path": os.environ.get("CONFIG_PATH") or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "config.json"
            ),
            "log_file": get_log_file_path(),
            "recent_logs": get_recent_logs(),
        }

    def format_diagnostics_text(self, diag: dict | None = None) -> str:
        d = diag or self.get_diagnostics()
        lines = [
            f"MediaPlayer Companion Bridge v{d['version']} (PID {d['pid']} on {d['hostname']})",
            f"Service Status : {d['status'].upper()} (tray={d['tray_state']})",
            f"Listen Address : {d['host']}:{d['port']}",
            f"Web UI URL     : {d['ui_url']}",
        ]
        if d["lan_urls"]:
            lines.append(f"LAN URLs       : {', '.join(d['lan_urls'])}")
        lines.extend(
            [
                f"Uptime         : {d['uptime_seconds']}s (restarts: {d['restart_count']})",
                f"Target Monitor : {d['target_monitor']}",
                f"Active Monitor : {_format_monitor_short(d['active_monitor'])}",
                f"Monitor Error  : {d['monitor_error'] or 'None'}",
                f"Current Still  : {(d['current_still'] or {}).get('name', 'None')}",
                f"Last Error     : {d['last_error'] or 'None'}",
                f"Config Path    : {d['config_path']}",
                f"Stills Dir     : {d['stills_dir']}",
                f"Cache Dir      : {d['cache_dir']}",
                f"Log File       : {d['log_file']}",
                "",
                f"Detected Monitors ({len(d['monitors'])}):",
            ]
        )
        for m in d["monitors"]:
            flags = []
            if m.get("primary"):
                flags.append("PRIMARY")
            if m.get("active"):
                flags.append("active")
            flag_str = f" [{' '.join(flags)}]" if flags else ""
            lines.append(
                f"  [{m.get('index')}] {m.get('width')}x{m.get('height')} @ ({m.get('left')},{m.get('top')}){flag_str} - {m.get('id')}"
            )
        lines.append("")
        lines.append("Recent Logs:")
        lines.extend(d["recent_logs"][-80:] or ["  (no logs captured yet)"])
        return "\n".join(lines)


def _format_monitor_short(mon: dict | None) -> str:
    if not mon:
        return "None"
    tag = "Primary" if mon.get("primary") else "Extended"
    return f"[{mon.get('index')}] {mon.get('width')}x{mon.get('height')} ({tag})"


class DiagnosticsWindowManager:
    """Opens a native Tkinter diagnostics & log viewer window on demand."""

    def __init__(self, controller: BridgeServerController, on_restart_callback=None):
        self._controller = controller
        self._on_restart = on_restart_callback
        self._lock = threading.Lock()
        self._open = False
        self._root = None

    def show(self) -> None:
        if tk is None:
            log_path = get_log_file_path()
            if log_path:
                _open_path_in_os(log_path)
            return

        with self._lock:
            if self._open and self._root is not None:
                try:
                    self._root.after(0, self._bring_to_front)
                    return
                except Exception:
                    pass
            self._open = True

        threading.Thread(target=self._run_window, name="DiagnosticsUI", daemon=True).start()

    def _bring_to_front(self) -> None:
        if self._root is not None:
            self._root.deiconify()
            self._root.lift()
            self._root.focus_force()

    def _run_window(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("MediaPlayer Companion Bridge — Diagnostics")
        root.geometry("820x600")
        root.minsize(640, 460)

        header_frame = ttk.Frame(root, padding=10)
        header_frame.pack(fill=tk.X)

        status_var = tk.StringVar(value="Loading diagnostics...")
        status_label = ttk.Label(header_frame, textvariable=status_var, font=("Segoe UI", 11, "bold"))
        status_label.pack(side=tk.LEFT)

        btn_frame = ttk.Frame(root, padding=(10, 0, 10, 8))
        btn_frame.pack(fill=tk.X)

        text_box = scrolledtext.ScrolledText(root, wrap=tk.NONE, font=("Consolas", 9))
        text_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        text_box.configure(state=tk.DISABLED)

        def refresh() -> None:
            diag = self._controller.get_diagnostics(check_http=True)
            state_map = {
                "ok": f"HEALTHY — Running on port {diag['port']}",
                "warning": f"WARNING — Running on port {diag['port']} (check monitor/health)",
                "error": f"ERROR — Server {diag['status']} ({diag['last_start_error'] or 'check logs'})",
            }
            status_var.set(state_map.get(diag["tray_state"], diag["status"]))
            report = self._controller.format_diagnostics_text(diag)
            text_box.configure(state=tk.NORMAL)
            text_box.delete("1.0", tk.END)
            text_box.insert(tk.END, report)
            text_box.see(tk.END)
            text_box.configure(state=tk.DISABLED)

        def copy_report() -> None:
            report = self._controller.format_diagnostics_text()
            root.clipboard_clear()
            root.clipboard_append(report)
            status_var.set("Diagnostics report copied to clipboard.")

        def restart_srv() -> None:
            status_var.set("Restarting server...")
            root.update_idletasks()
            if self._on_restart:
                self._on_restart()
            else:
                self._controller.restart()
            refresh()

        ttk.Button(btn_frame, text="Refresh", command=refresh).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Open Web UI", command=lambda: webbrowser.open(self._controller.ui_url)).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_frame, text="Restart Service", command=restart_srv).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Copy Report", command=copy_report).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            btn_frame,
            text="Open Log File",
            command=lambda: _open_path_in_os(get_log_file_path() or ""),
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_frame, text="Close", command=root.destroy).pack(side=tk.RIGHT)

        def on_close() -> None:
            with self._lock:
                self._open = False
                self._root = None
            root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_close)
        refresh()
        root.mainloop()
        with self._lock:
            self._open = False
            self._root = None


class TrayDaemon:
    """System tray daemon wrapping BridgeServerController."""

    def __init__(self, controller: BridgeServerController):
        self.controller = controller
        self.diagnostics_ui = DiagnosticsWindowManager(controller, on_restart_callback=self._restart_action)
        self._icon = None
        self._stop_event = threading.Event()
        self._last_tray_state: str | None = None

    def _build_menu(self):
        return pystray.Menu(
            pystray.MenuItem(lambda _item: self._status_line(), None, enabled=False),
            pystray.MenuItem(lambda _item: self._monitor_line(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open Web UI", self._open_ui, default=True),
            pystray.MenuItem("Diagnose...", self._open_diagnostics),
            pystray.MenuItem("Restart Service", self._restart_action),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quick Actions",
                pystray.Menu(
                    pystray.MenuItem("Clear / Black Still", self._clear_black),
                    pystray.MenuItem("Open Stills Folder", self._open_stills),
                    pystray.MenuItem("Open Log File", self._open_log),
                    pystray.MenuItem("Open Config File", self._open_config),
                ),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Close / Exit", self._exit_action),
        )

    def _status_line(self) -> str:
        diag = self.controller.get_diagnostics()
        still_name = (diag.get("current_still") or {}).get("name")
        if diag["status"] == "running":
            base = f"Running on :{diag['port']}"
            return f"{base} | Still: {still_name}" if still_name else base
        return f"Error ({diag.get('last_start_error') or diag['status']})"

    def _monitor_line(self) -> str:
        diag = self.controller.get_diagnostics()
        if diag.get("active_monitor"):
            return f"Monitor: {_format_monitor_short(diag['active_monitor'])}"
        return f"Monitor: {diag.get('monitor_error') or 'Not detected'}"

    def _open_ui(self, _icon=None, _item=None) -> None:
        webbrowser.open(self.controller.ui_url)

    def _open_diagnostics(self, _icon=None, _item=None) -> None:
        self.diagnostics_ui.show()

    def _restart_action(self, _icon=None, _item=None) -> None:
        ok = self.controller.restart()
        self._refresh_icon(force=True)
        if self._icon is not None:
            try:
                if ok:
                    self._icon.notify(f"Listening on {self.controller.ui_url}", "MediaPlayer Bridge Restarted")
                else:
                    self._icon.notify("Failed to start — click Diagnose... for details", "MediaPlayer Bridge Error")
            except Exception:
                pass

    def _clear_black(self, _icon=None, _item=None) -> None:
        try:
            connect_host = _resolve_connect_host(self.controller.config.get("host"))
            requests.post(f"http://{connect_host}:{self.controller.port}/black", timeout=3.0)
        except Exception as exc:
            logger.warning("Tray clear/black failed: %s", exc)
        self._refresh_icon(force=True)

    def _open_stills(self, _icon=None, _item=None) -> None:
        stills_dir = self.controller.config.get("stills_dir")
        if stills_dir:
            os.makedirs(stills_dir, exist_ok=True)
            _open_path_in_os(stills_dir)

    def _open_log(self, _icon=None, _item=None) -> None:
        log_path = get_log_file_path()
        if log_path:
            _open_path_in_os(log_path)

    def _open_config(self, _icon=None, _item=None) -> None:
        cfg_path = self.controller.get_diagnostics().get("config_path")
        if cfg_path and os.path.exists(cfg_path):
            _open_path_in_os(cfg_path)

    def _exit_action(self, _icon=None, _item=None) -> None:
        logger.info("Exit requested from system tray")
        self._stop_event.set()
        self.controller.stop()
        if self._icon is not None:
            self._icon.stop()

    def _refresh_icon(self, force: bool = False) -> None:
        if self._icon is None:
            return
        diag = self.controller.get_diagnostics()
        state = diag["tray_state"]
        if force or state != self._last_tray_state:
            self._last_tray_state = state
            self._icon.icon = create_tray_icon_image(state)
        if diag["status"] == "running":
            mon = _format_monitor_short(diag.get("active_monitor"))
            self._icon.title = f"MediaPlayer Bridge (:{diag['port']}) — {mon}"
        else:
            self._icon.title = f"MediaPlayer Bridge — ERROR (click Diagnose)"
        try:
            self._icon.update_menu()
        except Exception:
            pass

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(timeout=5.0):
            try:
                self._refresh_icon()
            except Exception:
                pass

    def run(self) -> None:
        if pystray is None:
            raise RuntimeError("pystray is not installed; run with --no-tray or install pystray")

        started = self.controller.start()
        diag = self.controller.get_diagnostics()
        self._last_tray_state = diag["tray_state"]

        self._icon = pystray.Icon(
            "mediaplayer-companion-bridge",
            icon=create_tray_icon_image(diag["tray_state"]),
            title=f"MediaPlayer Bridge (:{diag['port']})",
            menu=self._build_menu(),
        )

        def setup(icon) -> None:
            icon.visible = True
            self._refresh_icon(force=True)
            if not started:
                try:
                    icon.notify(
                        f"Port {diag['port']} error: {diag['last_start_error']}. Right-click -> Diagnose...",
                        "MediaPlayer Bridge Error",
                    )
                except Exception:
                    pass
            threading.Thread(target=self._monitor_loop, name="TrayStatusMonitor", daemon=True).start()

        self._icon.run(setup=setup)
