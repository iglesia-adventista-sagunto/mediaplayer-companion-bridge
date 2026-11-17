import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import setup_wizard


class TestSetupWizard(unittest.TestCase):
    def test_non_interactive_creates_config_from_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            template_path = os.path.join(tmp, "config.template.json")
            config_path = os.path.join(tmp, "config.json")
            stills_dir = os.path.join(tmp, "stills")
            with open(template_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "port": 5002,
                        "host": "0.0.0.0",
                        "monitor": "extended",
                        "position": "fill",
                        "stills_dir": stills_dir,
                        "presets": [
                            {"name": "latest-thumbnail", "label": "Latest Thumbnail", "url": "http://127.0.0.1:5001/latest"},
                            {"name": "black", "label": "Black Still", "color": "#000000"},
                        ],
                    },
                    f,
                )

            cfg = setup_wizard.run_wizard(
                config_path=config_path,
                template_path=template_path,
                non_interactive=True,
                install_service=False,
                print_fn=lambda *_: None,
                monitors=[],
            )
            self.assertTrue(os.path.isfile(config_path))
            self.assertTrue(os.path.isdir(stills_dir))
            self.assertEqual(cfg["port"], 5002)
            self.assertEqual(len(cfg["presets"]), 2)

    def test_interactive_prompts_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            template_path = os.path.join(tmp, "config.template.json")
            with open(template_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "port": 5002,
                        "host": "0.0.0.0",
                        "monitor": "extended",
                        "position": "fill",
                        "stills_dir": "./stills",
                        "presets": [
                            {"name": "latest-thumbnail", "label": "Latest Thumbnail", "url": "http://127.0.0.1:5001/latest"},
                            {"name": "black", "label": "Black Still", "color": "#000000"},
                        ],
                    },
                    f,
                )

            answers = iter([
                "not-a-port",   # invalid port -> retry
                "5010",         # valid port
                "1",            # monitor index
                "badmode",      # invalid position -> retry
                "fit",          # valid position
                "http://192.168.1.50:5001/latest",  # thumbnail URL
                "",             # keep default stills_dir
                "n",            # do not run install-windows.ps1
            ])

            cfg = setup_wizard.run_wizard(
                config_path=config_path,
                template_path=template_path,
                non_interactive=False,
                install_service=False,
                input_fn=lambda _prompt: next(answers),
                print_fn=lambda *_: None,
                monitors=[{"index": 0, "width": 1920, "height": 1080, "primary": True, "id": "MON0"}],
            )
            self.assertEqual(cfg["port"], 5010)
            self.assertEqual(cfg["monitor"], "1")
            self.assertEqual(cfg["position"], "fit")
            self.assertEqual(cfg["presets"][0]["url"], "http://192.168.1.50:5001/latest")
            self.assertEqual(cfg["presets"][1]["name"], "black")

    def test_ensure_venv_repairs_missing_deps_and_uses_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            venv_dir = os.path.join(tmp, "venv")
            scripts_dir = os.path.join(venv_dir, "Scripts")
            os.makedirs(scripts_dir, exist_ok=True)
            venv_py = os.path.join(scripts_dir, "python.exe")
            with open(venv_py, "w", encoding="utf-8") as f:
                f.write("")

            req_path = os.path.join(tmp, "requirements.txt")
            with open(req_path, "wb") as f:
                f.write(b"Flask>=3.0.0\n")
            expected_hash = hashlib.sha256(b"Flask>=3.0.0\n").hexdigest()

            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with mock.patch("setup_wizard.subprocess.run", side_effect=fake_run):
                result = setup_wizard.ensure_venv(venv_dir, req_path, print_fn=lambda *_: None)

            self.assertEqual(result, venv_py)
            self.assertTrue(any(cmd[:3] == [venv_py, "-m", "pip"] and "install" in cmd and "-r" in cmd for cmd in calls))
            stamp_path = os.path.join(venv_dir, ".requirements.sha256")
            with open(stamp_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read().strip(), expected_hash)

            # Second call with matching stamp and successful import probe should skip pip install
            calls.clear()
            with mock.patch("setup_wizard.subprocess.run", side_effect=fake_run):
                setup_wizard.ensure_venv(venv_dir, req_path, print_fn=lambda *_: None)

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], venv_py)
            self.assertEqual(calls[0][1], "-c")

    def test_install_windows_autostart_registers_with_windows_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "config.json")
            template_path = os.path.join(tmp, "config.template.json")
            venv_dir = os.path.join(tmp, "venv")
            cache_dir = os.path.join(tmp, "cache")
            with open(template_path, "w", encoding="utf-8") as f:
                json.dump({"port": 5002, "host": "0.0.0.0"}, f)

            ps_commands = []

            def fake_run(cmd, **kwargs):
                if cmd and cmd[0] == "powershell.exe":
                    ps_commands.append(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")

            with (
                mock.patch("setup_wizard.sys.platform", "win32"),
                mock.patch("setup_wizard.ensure_venv", return_value=os.path.join(venv_dir, "Scripts", "python.exe")),
                mock.patch("setup_wizard.subprocess.run", side_effect=fake_run),
                mock.patch("setup_wizard.time.sleep"),
            ):
                messages = []
                setup_wizard.install_windows_autostart(
                    config_path=config_path,
                    template_path=template_path,
                    venv_dir=venv_dir,
                    cache_dir=cache_dir,
                    start_now=True,
                    print_fn=messages.append,
                )

            register_cmds = [c for c in ps_commands if "Register-ScheduledTask" in c]
            self.assertEqual(len(register_cmds), 1)
            self.assertIn("WindowsIdentity", register_cmds[0])
            self.assertNotIn("$env:USERNAME", register_cmds[0])
            self.assertTrue(any("Started 'MediaPlayerCompanionBridge'" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
