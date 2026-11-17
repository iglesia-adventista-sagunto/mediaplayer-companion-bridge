import ctypes
import os
import sys
import threading
import uuid
from ctypes import wintypes

POSITION_TO_CODE = {
    "center": 0,
    "tile": 1,
    "stretch": 2,
    "fit": 3,
    "fill": 4,
    "span": 5,
}
CODE_TO_POSITION = {v: k for k, v in POSITION_TO_CODE.items()}

CLSID_DESKTOP_WALLPAPER = "{C2CF3110-460E-4fc1-B9D0-8A1C0C9CC4BD}"
IID_IDESKTOP_WALLPAPER = "{B92B56A9-8B55-4E14-9A89-0199BBB6F93B}"

COINIT_APARTMENTTHREADED = 0x2
CLSCTX_LOCAL_SERVER = 0x4
S_OK = 0x0
S_FALSE = 0x1
RPC_E_CHANGED_MODE = 0x80010106


class WallpaperError(RuntimeError):
    """Base error for desktop wallpaper operations."""


class MonitorNotFoundError(WallpaperError):
    """Raised when the requested monitor cannot be resolved or is inactive."""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _make_guid(guid_str: str) -> _GUID:
    u = uuid.UUID(guid_str)
    return _GUID(
        u.time_low,
        u.time_mid,
        u.time_hi_version,
        (ctypes.c_ubyte * 8)(*u.bytes[8:]),
    )


def _check_hr(hr: int, action: str) -> None:
    if hr < 0:
        code = hr & 0xFFFFFFFF
        raise WallpaperError(f"{action} failed with HRESULT 0x{code:08X}")


class WindowsDesktopWallpaperBackend:
    """Windows IDesktopWallpaper COM backend using ctypes (no external COM packages)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise WallpaperError("WindowsDesktopWallpaperBackend is only supported on Windows")
        self._lock = threading.Lock()
        self._ole32 = ctypes.windll.ole32
        self._clsid = _make_guid(CLSID_DESKTOP_WALLPAPER)
        self._iid = _make_guid(IID_IDESKTOP_WALLPAPER)

    def _with_com(self, fn):
        with self._lock:
            hr_init = self._ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            should_uninit = hr_init in (S_OK, S_FALSE)
            if hr_init < 0 and (hr_init & 0xFFFFFFFF) != RPC_E_CHANGED_MODE:
                _check_hr(hr_init, "CoInitializeEx")

            ppv = ctypes.c_void_p()
            try:
                hr = self._ole32.CoCreateInstance(
                    ctypes.byref(self._clsid),
                    None,
                    CLSCTX_LOCAL_SERVER,
                    ctypes.byref(self._iid),
                    ctypes.byref(ppv),
                )
                _check_hr(hr, "CoCreateInstance(IDesktopWallpaper)")
                vtable = ctypes.cast(
                    ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))[0],
                    ctypes.POINTER(ctypes.c_void_p),
                )
                try:
                    return fn(ppv, vtable)
                finally:
                    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
                    release(ppv)
            finally:
                if should_uninit:
                    self._ole32.CoUninitialize()

    def list_monitors(self) -> list[dict]:
        def _run(ppv, vtable):
            get_count = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)
            )(vtable[6])
            get_path_at = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.UINT, ctypes.POINTER(wintypes.LPWSTR)
            )(vtable[5])
            get_rect = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(wintypes.RECT)
            )(vtable[7])
            get_wall = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)
            )(vtable[4])

            count = wintypes.UINT()
            _check_hr(get_count(ppv, ctypes.byref(count)), "GetMonitorDevicePathCount")

            monitors = []
            for idx in range(count.value):
                mid = wintypes.LPWSTR()
                hr_path = get_path_at(ppv, idx, ctypes.byref(mid))
                if hr_path < 0:
                    continue
                monitor_id = mid.value or ""
                self._ole32.CoTaskMemFree(mid)

                rect = wintypes.RECT()
                hr_rect = get_rect(ppv, monitor_id, ctypes.byref(rect))
                active = hr_rect >= 0 and (rect.right - rect.left) > 0 and (rect.bottom - rect.top) > 0

                wall = wintypes.LPWSTR()
                hr_wall = get_wall(ppv, monitor_id, ctypes.byref(wall))
                wallpaper_path = wall.value if hr_wall >= 0 and wall.value else ""
                if wall.value:
                    self._ole32.CoTaskMemFree(wall)

                width = max(0, rect.right - rect.left) if hr_rect >= 0 else 0
                height = max(0, rect.bottom - rect.top) if hr_rect >= 0 else 0
                monitors.append(
                    {
                        "index": idx,
                        "id": monitor_id,
                        "active": active,
                        "primary": active and rect.left == 0 and rect.top == 0,
                        "left": rect.left if hr_rect >= 0 else 0,
                        "top": rect.top if hr_rect >= 0 else 0,
                        "right": rect.right if hr_rect >= 0 else 0,
                        "bottom": rect.bottom if hr_rect >= 0 else 0,
                        "width": width,
                        "height": height,
                        "wallpaper": wallpaper_path,
                    }
                )
            return monitors

        return self._with_com(_run)

    def get_wallpaper(self, monitor_id: str) -> str:
        def _run(ppv, vtable):
            get_wall = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPWSTR)
            )(vtable[4])
            wall = wintypes.LPWSTR()
            _check_hr(get_wall(ppv, monitor_id, ctypes.byref(wall)), "GetWallpaper")
            val = wall.value or ""
            if wall.value:
                self._ole32.CoTaskMemFree(wall)
            return val

        return self._with_com(_run)

    def set_wallpaper(self, monitor_id: str, image_path: str) -> None:
        abs_path = os.path.abspath(image_path)
        if not os.path.isfile(abs_path):
            raise FileNotFoundError(f"Wallpaper image not found: {abs_path}")

        def _run(ppv, vtable):
            set_wall = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR
            )(vtable[3])
            _check_hr(set_wall(ppv, monitor_id, abs_path), "SetWallpaper")

        self._with_com(_run)

    def get_position(self) -> str:
        def _run(ppv, vtable):
            get_pos = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)
            )(vtable[11])
            pos = ctypes.c_int()
            _check_hr(get_pos(ppv, ctypes.byref(pos)), "GetPosition")
            return CODE_TO_POSITION.get(pos.value, f"unknown({pos.value})")

        return self._with_com(_run)

    def set_position(self, position: str) -> str:
        key = str(position).strip().lower()
        if key not in POSITION_TO_CODE:
            valid = ", ".join(POSITION_TO_CODE.keys())
            raise ValueError(f"Invalid position '{position}'. Valid options: {valid}")
        code = POSITION_TO_CODE[key]

        def _run(ppv, vtable):
            set_pos = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p, ctypes.c_int
            )(vtable[10])
            _check_hr(set_pos(ppv, code), "SetPosition")
            return key

        return self._with_com(_run)


def resolve_monitor(monitors: list[dict], target: str | int | None = "extended") -> dict:
    """Resolve a target selector ('extended', 'primary', index, or ID substring) to an active monitor."""
    active_monitors = [m for m in monitors if m.get("active")]
    if not active_monitors:
        raise MonitorNotFoundError("No active display monitors detected")

    if target is None or str(target).strip() == "" or str(target).strip().lower() == "extended":
        extended = [m for m in active_monitors if not m.get("primary")]
        if not extended:
            raise MonitorNotFoundError(
                "No active extended (non-primary) monitor detected. "
                "Connect/extend the Blackmagic display or set monitor='primary' / index."
            )
        return extended[0]

    selector = str(target).strip()
    lower = selector.lower()

    if lower == "primary":
        primaries = [m for m in active_monitors if m.get("primary")]
        if not primaries:
            return active_monitors[0]
        return primaries[0]

    if selector.isdigit():
        idx = int(selector)
        for m in monitors:
            if m.get("index") == idx:
                if not m.get("active"):
                    raise MonitorNotFoundError(f"Monitor index {idx} exists but is not active")
                return m
        raise MonitorNotFoundError(f"Monitor index {idx} not found (total: {len(monitors)})")

    matches = [m for m in active_monitors if lower in m.get("id", "").lower()]
    if matches:
        return matches[0]

    raise MonitorNotFoundError(f"No active monitor matching '{target}'")
