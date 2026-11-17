import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

from wallpaper_service import POSITION_TO_CODE, WindowsDesktopWallpaperBackend

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATE_CONFIG_PATH = os.path.join(BASE_DIR, "config.template.json")
DEFAULT_TASK_NAME = "MediaPlayerCompanionBridge"


def load_base_config(config_path: str = DEFAULT_CONFIG_PATH, template_path: str = TEMPLATE_CONFIG_PATH) -> dict:
    """Load existing config.json, falling back to config.template.json or built-in defaults."""
    for path in (config_path, template_path):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    return {
        "port": 5002,
        "host": "0.0.0.0",
        "monitor": "extended",
        "position": "fill",
        "stills_dir": "./stills",
        "cache_dir": "./cache",
        "max_cache_files": 20,
        "url_timeout_seconds": 15,
        "presets": [
            {
                "name": "latest-thumbnail",
                "label": "Latest Thumbnail",
                "url": "http://127.0.0.1:5001/latest",
            },
            {
                "name": "black",
                "label": "Black Still",
                "color": "#000000",
            },
        ],
    }


def detect_monitors() -> list[dict]:
    """Detect Windows displays via IDesktopWallpaper COM; returns [] if unavailable."""
    if sys.platform != "win32":
        return []
    try:
        return WindowsDesktopWallpaperBackend().list_monitors()
    except Exception:
        return []


def _prompt(text: str, default: str, input_fn=input) -> str:
    raw = input_fn(f"{text} [{default}]: ").strip()
    return raw if raw else default


def _prompt_port(default_port: int, input_fn=input, print_fn=print) -> int:
    while True:
        raw = _prompt("HTTP port", str(default_port), input_fn=input_fn)
        try:
            port = int(raw)
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass
        print_fn("Please enter a valid TCP port between 1 and 65535.")


def _prompt_position(default_pos: str, input_fn=input, print_fn=print) -> str:
    valid = ", ".join(POSITION_TO_CODE.keys())
    while True:
        val = _prompt(f"Scaling mode ({valid})", default_pos, input_fn=input_fn).lower()
        if val in POSITION_TO_CODE:
            return val
        print_fn(f"Invalid scaling mode '{val}'. Choose one of: {valid}")


def _prompt_monitor(default_monitor: str, monitors: list[dict], input_fn=input, print_fn=print) -> str:
    if monitors:
        print_fn("\nDetected displays:")
        for m in monitors:
            role = "Primary" if m.get("primary") else "Extended"
            print_fn(f"  [{m['index']}] {m.get('width', '?')}x{m.get('height', '?')} ({role}) - {m.get('id', '')}")
    else:
        print_fn("\nNo displays detected (or running non-Windows). You can still set 'extended', 'primary', or an index.")
    return _prompt("Target monitor ('extended', 'primary', index, or ID substring)", default_monitor, input_fn=input_fn)


def _update_thumbnail_preset(presets: list[dict], new_url: str) -> list[dict]:
    updated = [dict(p) for p in presets if isinstance(p, dict)]
    for preset in updated:
        if preset.get("name") == "latest-thumbnail":
            preset["url"] = new_url
            return updated
    updated.insert(
        0,
        {
            "name": "latest-thumbnail",
            "label": "Latest Thumbnail",
            "url": new_url,
        },
    )
    return updated


def _local_app_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "mediaplayer-companion-bridge")


def _ps_escape(value: str) -> str:
    return value.replace("'", "''")


def _stop_running_bridge_processes(task_name: str = DEFAULT_TASK_NAME) -> int:
    if sys.platform != "win32":
        return 0
    escaped_base = _ps_escape(BASE_DIR)
    ps_script = (
        f"$t = Get-ScheduledTask -TaskName '{_ps_escape(task_name)}' -ErrorAction SilentlyContinue; "
        f"if ($t) {{ Stop-ScheduledTask -TaskName '{_ps_escape(task_name)}' -ErrorAction SilentlyContinue }}; "
        "$stopped = 0; "
        "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.CommandLine -like '*app.py*' -and ($_.CommandLine -like '*mediaplayer-companion-bridge*' -or $_.CommandLine -like '*{escaped_base}*') }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $stopped++ }; "
        "Write-Output $stopped"
    )
    res = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_script],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return int((res.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _requirements_hash(req_path: str) -> str:
    if not os.path.exists(req_path):
        return ""
    with open(req_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_venv(
    venv_dir: str,
    req_path: str,
    python_exe: str | None = None,
    print_fn=print,
) -> str:
    """Ensure a working machine-local venv exists with all requirements installed."""
    venv_py = os.path.join(venv_dir, "Scripts", "python.exe")
    base_py = python_exe or sys.executable
    if base_py.lower().endswith("pythonw.exe"):
        sibling_py = os.path.join(os.path.dirname(base_py), "python.exe")
        if os.path.exists(sibling_py):
            base_py = sibling_py

    if not os.path.exists(venv_py):
        print_fn(f"Creating machine-local virtual environment at {venv_dir}...")
        os.makedirs(os.path.dirname(os.path.abspath(venv_dir)), exist_ok=True)
        subprocess.run([base_py, "-m", "venv", venv_dir], check=True)

    stamp_path = os.path.join(venv_dir, ".requirements.sha256")
    expected_hash = _requirements_hash(req_path)
    installed_hash = ""
    if os.path.exists(stamp_path):
        with open(stamp_path, "r", encoding="utf-8") as f:
            installed_hash = f.read().strip()

    deps_ok = False
    if expected_hash and installed_hash == expected_hash:
        probe = subprocess.run(
            [venv_py, "-c", "import flask, PIL, pystray, requests"],
            capture_output=True,
            check=False,
        )
        deps_ok = probe.returncode == 0

    if not deps_ok:
        print_fn(f"Installing Python dependencies into {venv_dir}...")
        subprocess.run([venv_py, "-m", "pip", "install", "--upgrade", "pip"], check=False)
        subprocess.run([venv_py, "-m", "pip", "install", "-r", req_path], check=True)
        if expected_hash:
            with open(stamp_path, "w", encoding="utf-8") as f:
                f.write(expected_hash)

    return venv_py


def ensure_config_exists(
    config_path: str = DEFAULT_CONFIG_PATH,
    template_path: str = TEMPLATE_CONFIG_PATH,
    print_fn=print,
) -> dict:
    """Create config.json from template/defaults if missing; never overwrites an existing config."""
    if os.path.exists(config_path):
        return load_base_config(config_path=config_path, template_path=template_path)
    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    if os.path.exists(template_path):
        shutil.copyfile(template_path, config_path)
        print_fn(f"Created {config_path} from {os.path.basename(template_path)}")
        return load_base_config(config_path=config_path, template_path=template_path)
    cfg = load_base_config(config_path=config_path, template_path=template_path)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    print_fn(f"Created {config_path} with default settings")
    return cfg


def install_windows_autostart(
    config_path: str = DEFAULT_CONFIG_PATH,
    template_path: str = TEMPLATE_CONFIG_PATH,
    task_name: str = DEFAULT_TASK_NAME,
    venv_dir: str | None = None,
    cache_dir: str | None = None,
    start_now: bool = True,
    print_fn=print,
) -> None:
    """Register a standard-user Windows logon Scheduled Task (no Administrator required)."""
    if sys.platform != "win32":
        raise RuntimeError("Windows auto-start installation is only supported on Windows.")

    cfg = ensure_config_exists(config_path=config_path, template_path=template_path, print_fn=print_fn)
    port = int(cfg.get("port", 5002))
    host = str(cfg.get("host") or "0.0.0.0").strip()
    ui_host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "[::]") else host

    app_local = _local_app_dir()
    venv_dir = venv_dir or os.path.join(app_local, "venv")
    cache_dir = cache_dir or os.path.join(app_local, "cache")
    req_path = os.path.join(BASE_DIR, "requirements.txt")
    app_path = os.path.join(BASE_DIR, "app.py")

    _stop_running_bridge_processes(task_name=task_name)

    venv_py = ensure_venv(venv_dir=venv_dir, req_path=req_path, print_fn=print_fn)

    pythonw_exe = os.path.join(venv_dir, "Scripts", "pythonw.exe")
    if not os.path.exists(pythonw_exe):
        pythonw_exe = venv_py

    os.makedirs(cache_dir, exist_ok=True)

    py_args = f'"{app_path}" --cache-dir "{cache_dir}"'
    ps_register = (
        f"$ErrorActionPreference = 'Stop'; "
        "$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        f"$Action = New-ScheduledTaskAction -Execute '{_ps_escape(pythonw_exe)}' "
        f"-Argument '{_ps_escape(py_args)}' -WorkingDirectory '{_ps_escape(BASE_DIR)}'; "
        "$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId; "
        "$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited; "
        "$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1); "
        f"Register-ScheduledTask -TaskName '{_ps_escape(task_name)}' -Action $Action -Trigger $Trigger "
        "-Principal $Principal -Settings $Settings -Force | Out-Null"
    )
    if start_now:
        ps_register += f"; Start-ScheduledTask -TaskName '{_ps_escape(task_name)}'"

    res = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_register],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"Failed to register Windows auto-start task '{task_name}': {detail}")

    print_fn(f"Registered Windows auto-start task '{task_name}' (runs at logon in interactive session).")
    if start_now:
        running = False
        escaped_base = _ps_escape(BASE_DIR)
        ps_check = (
            "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' OR Name = 'pythonw.exe'\" -ErrorAction SilentlyContinue | "
            f"Where-Object {{ $_.CommandLine -like '*app.py*' -and ($_.CommandLine -like '*mediaplayer-companion-bridge*' -or $_.CommandLine -like '*{escaped_base}*') }} | "
            "Measure-Object | Select-Object -ExpandProperty Count"
        )
        for _ in range(6):
            time.sleep(0.4)
            chk = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_check],
                capture_output=True,
                text=True,
                check=False,
            )
            try:
                if int((chk.stdout or "0").strip().splitlines()[-1]) > 0:
                    running = True
                    break
            except (ValueError, IndexError):
                pass
        if running:
            print_fn(f"Started '{task_name}'. Web UI: http://{ui_host}:{port}/ui")
        else:
            log_path = os.path.join(app_local, "logs", "bridge.log")
            print_fn(
                f"[WARNING] Task '{task_name}' was triggered, but no running bridge process was detected. "
                f"Check {log_path} or Task Scheduler history."
            )


def uninstall_windows_autostart(task_name: str = DEFAULT_TASK_NAME, print_fn=print) -> None:
    """Stop running bridge processes and remove the Windows logon Scheduled Task."""
    if sys.platform != "win32":
        raise RuntimeError("Windows auto-start removal is only supported on Windows.")

    ps_remove = (
        f"$t = Get-ScheduledTask -TaskName '{_ps_escape(task_name)}' -ErrorAction SilentlyContinue; "
        "if ($t) { "
        f"Stop-ScheduledTask -TaskName '{_ps_escape(task_name)}' -ErrorAction SilentlyContinue; "
        f"Unregister-ScheduledTask -TaskName '{_ps_escape(task_name)}' -Confirm:$false; "
        "Write-Output 'removed' "
        "} else { Write-Output 'missing' }"
    )
    res = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps_remove],
        capture_output=True,
        text=True,
        check=True,
    )
    status = (res.stdout or "").strip().splitlines()[-1] if res.stdout else "missing"
    if status == "removed":
        print_fn(f"Removed Windows auto-start task '{task_name}'.")
    else:
        print_fn(f"Windows auto-start task '{task_name}' was not registered.")

    stopped = _stop_running_bridge_processes(task_name=task_name)
    if stopped > 0:
        print_fn(f"Stopped {stopped} running MediaPlayer Companion Bridge process(es).")


def run_wizard(
    config_path: str = DEFAULT_CONFIG_PATH,
    template_path: str = TEMPLATE_CONFIG_PATH,
    non_interactive: bool = False,
    install_service: bool | None = None,
    input_fn=input,
    print_fn=print,
    monitors: list[dict] | None = None,
) -> dict:
    """Run the minimal setup wizard and write config.json."""
    cfg = load_base_config(config_path=config_path, template_path=template_path)
    detected = detect_monitors() if monitors is None else monitors

    presets = cfg.get("presets") if isinstance(cfg.get("presets"), list) else []
    thumb_url = "http://127.0.0.1:5001/latest"
    for p in presets:
        if isinstance(p, dict) and p.get("name") == "latest-thumbnail" and p.get("url"):
            thumb_url = str(p["url"])
            break

    if not non_interactive:
        print_fn("=== MediaPlayer Companion Bridge - Setup Wizard ===")
        print_fn("Press Enter to keep the [default] value.\n")

        cfg["port"] = _prompt_port(int(cfg.get("port", 5002)), input_fn=input_fn, print_fn=print_fn)
        cfg["monitor"] = _prompt_monitor(
            str(cfg.get("monitor", "extended")), detected, input_fn=input_fn, print_fn=print_fn
        )
        cfg["position"] = _prompt_position(
            str(cfg.get("position", "fill")).lower(), input_fn=input_fn, print_fn=print_fn
        )
        thumb_url = _prompt(
            "Thumbnail Companion Bridge URL (e.g. http://<LXC-IP>:5001/latest)",
            thumb_url,
            input_fn=input_fn,
        )
        cfg["stills_dir"] = _prompt("Local stills directory", str(cfg.get("stills_dir", "./stills")), input_fn=input_fn)

    cfg["presets"] = _update_thumbnail_preset(presets, thumb_url)

    os.makedirs(os.path.dirname(os.path.abspath(config_path)), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

    stills_dir = cfg.get("stills_dir", "./stills")
    if not os.path.isabs(stills_dir):
        stills_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(config_path)), stills_dir))
    os.makedirs(stills_dir, exist_ok=True)

    print_fn(f"\nSaved configuration to: {config_path}")
    print_fn(f"  Port     : {cfg['port']}")
    print_fn(f"  Monitor  : {cfg['monitor']}")
    print_fn(f"  Position : {cfg['position']}")
    print_fn(f"  Stills   : {cfg['stills_dir']}")
    print_fn(f"  Thumb URL: {thumb_url}")

    should_install = install_service
    if should_install is None and not non_interactive and sys.platform == "win32":
        ans = _prompt("\nEnable auto-start at Windows logon? (y/n)", "n", input_fn=input_fn)
        should_install = ans.lower() in ("y", "yes")

    if should_install and sys.platform == "win32":
        print_fn("\nConfiguring Windows auto-start...")
        install_windows_autostart(config_path=config_path, template_path=template_path, print_fn=print_fn)
    elif not non_interactive:
        print_fn("\nNext steps:")
        print_fn("  - Run 'start.bat' to start the bridge, or")
        print_fn("  - Run 'start.bat install' to enable auto-start at Windows logon.")

    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="Setup & Windows auto-start manager for MediaPlayer Companion Bridge")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.json to write")
    parser.add_argument("--non-interactive", action="store_true", help="Write defaults without prompting")
    parser.add_argument("--install", action="store_true", help="Enable Windows auto-start after saving config")
    parser.add_argument(
        "--install-only",
        action="store_true",
        help="Enable Windows auto-start without overwriting an existing config.json",
    )
    parser.add_argument("--uninstall", action="store_true", help="Remove Windows auto-start and stop running bridge")
    args = parser.parse_args()

    if args.uninstall:
        uninstall_windows_autostart()
        return

    if args.install_only:
        install_windows_autostart(config_path=args.config)
        return

    run_wizard(
        config_path=args.config,
        non_interactive=args.non_interactive,
        install_service=True if args.install else None,
    )


if __name__ == "__main__":
    main()
