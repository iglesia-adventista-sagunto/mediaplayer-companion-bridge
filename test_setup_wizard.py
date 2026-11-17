import json
import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
