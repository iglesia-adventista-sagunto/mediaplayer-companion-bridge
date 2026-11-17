import argparse
import json
import os
import subprocess
import sys

from wallpaper_service import POSITION_TO_CODE, WindowsDesktopWallpaperBackend

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
TEMPLATE_CONFIG_PATH = os.path.join(BASE_DIR, "config.template.json")
INSTALL_PS1_PATH = os.path.join(BASE_DIR, "install-windows.ps1")


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
    if should_install is None and not non_interactive and sys.platform == "win32" and os.path.exists(INSTALL_PS1_PATH):
        ans = _prompt("\nInstall/update Windows auto-start task & firewall rule now? (y/n)", "n", input_fn=input_fn)
        should_install = ans.lower() in ("y", "yes")

    if should_install and sys.platform == "win32" and os.path.exists(INSTALL_PS1_PATH):
        print_fn("\nRunning install-windows.ps1...")
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                INSTALL_PS1_PATH,
            ],
            check=True,
        )
    elif not non_interactive:
        print_fn("\nNext steps:")
        print_fn("  - Run 'start.bat' to start in foreground, or")
        print_fn("  - Run 'install-windows.bat' to register auto-start on Windows logon.")

    return cfg


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal setup wizard for MediaPlayer Companion Bridge")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Path to config.json to write")
    parser.add_argument("--non-interactive", action="store_true", help="Write defaults without prompting")
    parser.add_argument("--install", action="store_true", help="Also run install-windows.ps1 after saving config")
    args = parser.parse_args()

    run_wizard(
        config_path=args.config,
        non_interactive=args.non_interactive,
        install_service=True if args.install else None,
    )


if __name__ == "__main__":
    main()
