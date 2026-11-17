# MediaPlayer Companion Bridge

HTTP REST API and Web UI to mimic an **ATEM Media Player (Still Images)** by controlling the Windows **extended desktop background** (e.g. a Blackmagic output / second display) using the native Windows `IDesktopWallpaper` COM interface.

Designed for **Bitfocus Companion**, **Stream Deck**, **OBS**, and integration with **thumbnail-companion-bridge**.

---

## Why This Exists

ATEM Television Studio hardware/API has limitations when automating still image uploads or restoring media pool files without the ATEM Software Control GUI. By routing an extended Windows display into an ATEM input (or Blackmagic output), this bridge lets you instantaneously switch, upload, or fetch still images over HTTP—without stealing focus from your primary operator screen.

---

## Quick Start (Windows Host or Win11 VM)

Unlike `tuya-companion-bridge` and `thumbnail-companion-bridge` (which run in Proxmox LXC containers), **this service must run directly on the Windows machine** driving the extended display (either the **Host Machine** or the **Win11 VM**) in an **interactive logged-in user session** (Windows `IDesktopWallpaper` COM cannot access the desktop from a Session 0 background service).

### Step 1: Quick Start (Recommended)

Double-click **`start.bat`** (or run it from a terminal) to automatically create the machine-local Python `venv`, initialize `config.json` from `config.template.json` if missing, and launch the bridge with its System Tray icon:

```bat
.\start.bat
```

### Step 2: Interactive Setup Wizard (Optional)

Run the setup wizard to detect your connected displays and customize `config.json`:

```bat
.\start.bat setup
```

Or directly with Python:

```powershell
python setup_wizard.py
```

The wizard guides you through:
- **HTTP port** (default `5002`)
- **Target monitor** (auto-lists detected Windows displays, or `extended` / `primary` / index)
- **Scaling mode** (`fill`, `fit`, `stretch`, `center`, `tile`, `span`)
- **`thumbnail-companion-bridge` URL** for the `latest-thumbnail` preset
- **Local stills directory** (`./stills`)
- Optionally enabling **Windows logon auto-start**

Pass `--non-interactive` to write defaults from `config.template.json` without prompting.

### Auto-start at Windows Logon (No Administrator Required)

To register and start a standard-user **Task Scheduler** task (`MediaPlayerCompanionBridge`) that runs `pythonw.exe` at logon in your interactive desktop session:

```bat
.\start.bat install
```

This creates a **machine-local** Python `venv` and `cache` under `%LOCALAPPDATA%\mediaplayer-companion-bridge` (so multiple Windows machines can safely share the same NAS folder without conflicting `venv` or `cache` files) and starts the tray daemon immediately.

To stop the bridge and remove the scheduled task later:

```bat
.\start.bat uninstall
```

### System Tray & Diagnostics

`start.bat` (and the auto-start task) launches a **System Tray icon** (dark monitor badge with a green/amber/red status dot) so you can easily:
- **Open Web UI** (double-click tray icon or click *Open Web UI* — opens the URL configured in `config.json`)
- **Diagnose...** (opens a live diagnostics & log viewer window with monitor detection, URLs, errors, and rotating logs at `%LOCALAPPDATA%\mediaplayer-companion-bridge\logs\bridge.log`)
- **Restart Service** (reloads `config.json` and restarts the HTTP server in-place)
- **Close / Exit** (cleanly shuts down the HTTP server and tray daemon)

Pass `--no-tray` (or set `NO_TRAY=1`) to run headless without a tray icon:

```bat
.\start.bat --no-tray
```

Open the Web UI at: `http://<host-or-vm-ip>:5002/ui`

> **Tip (Multi-Machine / LXC Integration):** In `config.json`, update the `latest-thumbnail` preset `url` from `127.0.0.1:5001` to the IP/hostname of your `thumbnail-companion-bridge` LXC container (e.g. `http://192.168.1.50:5001/latest`). You can also override `CONFIG_PATH` or `PORT` per machine via environment variables if both the Host and Win11 VM run simultaneously.

---

## Configuration (`config.json`)

Copy `config.template.json` to `config.json` and customize as needed:

```json
{
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
      "url": "http://127.0.0.1:5001/latest"
    },
    {
      "name": "black",
      "label": "Black Still",
      "color": "#000000"
    }
  ]
}
```

### `monitor` selectors
- `"extended"` (default): Automatically targets the first active non-primary monitor.
- `"primary"`: Targets the primary monitor.
- `"0"`, `"1"`, ...: Targets a specific monitor index from `GET /monitors`.
- Any substring of the Windows monitor device path (e.g. `"HXA0000"` or `"BMD"`).

---

## HTTP API Endpoints

Both `GET` and `POST` are supported on action endpoints for easy **Bitfocus Companion** (`Generic HTTP`) integration:

| Endpoint | Method(s) | Description |
| --- | --- | --- |
| `/` or `/ui` | `GET` | Operator Web UI |
| `/health` | `GET` | Healthcheck & monitor status |
| `/status` | `GET` | Full JSON state, monitors, stills, and presets |
| `/monitors` | `GET` | List all detected Windows displays & rects |
| `/monitor/<target>` | `GET`, `POST` | Switch target monitor (`extended`, `0`, `1`, or ID substring) |
| `/position/<mode>` | `GET`, `POST` | Set scaling (`fill`, `fit`, `stretch`, `center`, `tile`, `span`) |
| `/stills` | `GET` | List local stills in `stills/` and configured presets |
| `/still/<name>` | `GET`, `POST` | Apply a still from `stills/` (with or without extension) or preset |
| `/preset/<name>` | `GET`, `POST` | Apply a named preset (`url`, `file`, or `color`) |
| `/url?url=...` | `GET`, `POST` | Download an image from URL (e.g. `http://127.0.0.1:5001/latest`) and apply |
| `/upload` | `POST` | Upload an image (`multipart/form-data` `file` or raw body); supports `name`, `save` (`true`/`false`), `apply` (`true`/`false`) |
| `/clear` or `/black` | `GET`, `POST` | Apply a solid black image (or `?color=#RRGGBB`) |
| `/current` | `GET` | JSON info about the currently active still |
| `/current/name` | `GET` | Plaintext name of the current still (for Companion feedback) |
| `/current/image` | `GET` | Serves the currently active still image (`Cache-Control: no-store`) |
| `/stills/<filename>` | `DELETE` | Delete a still file from `stills/` |

---

## Running Tests

```powershell
& "$env:LOCALAPPDATA\mediaplayer-companion-bridge\venv\Scripts\python.exe" -m unittest discover -v
```

*(Runs all tests in `tests/`.)*

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)** — see [LICENSE](LICENSE) for details.
