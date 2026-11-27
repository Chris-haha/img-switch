"""Regressions for configuration safety, discovery and relay output integrity."""
import base64
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import app
sys.path[:0] = [str(Path(__file__).parent / "engine/scripts"),
                str(Path(__file__).parent / "engine/tests")]
import failovercfg as cfg
import failover_imagegen_tool as engine
from mock_relay import make_png


def source(name, base="http://127.0.0.1:9/v1"):
    return {"name": name, "base_url": base, "api_key": "audit-fake-key", "model": "test-image"}


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.json"
        cfg.save_config({"sources": [source("A")]}, str(self.path))

    def test_invalid_existing_config_is_never_replaced(self):
        original = b'{"sources": invalid'
        self.path.write_bytes(original)
        with self.assertRaises(RuntimeError):
            cfg.save_config({"sources": []}, str(self.path))
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_field_types_are_reported_as_config_errors(self):
        for key in ("name", "base_url", "api_key"):
            with self.subTest(key=key):
                broken = source("A")
                broken[key] = ["invalid"]
                raw = json.dumps({"sources": [broken]}).encode()
                self.path.write_bytes(raw)
                with self.assertRaises(RuntimeError):
                    cfg.load_config(str(self.path))
                self.assertEqual(self.path.read_bytes(), raw)

    def test_stale_reader_cannot_overwrite_another_save(self):
        first = cfg.load_config(str(self.path))
        second = cfg.load_config(str(self.path))
        first["sources"].append(source("B"))
        cfg.save_config(first, str(self.path))
        second["failover"] = False
        with self.assertRaises(RuntimeError):
            cfg.save_config(second, str(self.path))
        self.assertEqual([s["name"] for s in cfg.load_config(str(self.path))["sources"]], ["A", "B"])

    def test_two_first_time_windows_cannot_replace_each_others_config(self):
        path = self.path.parent / "new.json"
        first, second = cfg.default_config(), cfg.default_config()
        first["sources"].append(source("first-window"))
        cfg.save_config(first, str(path))
        second["sources"].append(source("second-window"))
        with self.assertRaises(RuntimeError):
            cfg.save_config(second, str(path))
        self.assertEqual(cfg.load_config(str(path))["sources"][0]["name"], "first-window")

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_saving_keeps_secrets_private(self):
        self.path.chmod(0o600)
        cfg.save_config(cfg.load_config(str(self.path)), str(self.path))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX lock exercised here; Windows CI uses msvcrt")
    def test_another_writer_lock_prevents_save(self):
        import fcntl
        original = self.path.read_bytes()
        with open(str(self.path) + ".lock", "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(RuntimeError):
                cfg.save_config({"sources": []}, str(self.path))
            self.assertEqual(self.path.read_bytes(), original)


class GuiSafetyTests(unittest.TestCase):
    def run_gui(self, action):
        import tkinter as tk
        from tkinter import messagebox
        with tempfile.TemporaryDirectory() as work:
            user_dir = Path(work) / "user"
            user_dir.mkdir()
            config_dir = Path(work) / "config"
            path = config_dir / "config.json"
            cfg.save_config({"sources": [source(n) for n in ("A", "B", "C")]}, str(path))
            errors = []
            def mainloop(window):
                try:
                    window.update()
                    window.show_page("sources")
                    action(window, path, errors)
                finally:
                    window.destroy()
            with mock.patch.object(app, "home", return_value=str(user_dir)), \
                 mock.patch.object(app, "appdata_dir", return_value=str(config_dir)), \
                 mock.patch.object(tk.Tk, "mainloop", mainloop), \
                 mock.patch.object(messagebox, "askyesno", return_value=True), \
                 mock.patch.object(messagebox, "showerror", side_effect=lambda *a, **k: errors.append(a)):
                app.cmd_gui()

    def test_delete_before_edited_source_does_not_overwrite_next_source(self):
        def exercise(window, path, errors):
            sp = window.pages["sources"]
            sp._edit(1)
            sp.vars["name"].set("B-renamed")
            sp._remove(0)
            sp._save_form()
            self.assertEqual([s["name"] for s in cfg.load_config(str(path))["sources"]], ["B-renamed", "C"])
            self.assertEqual(errors, [])
        self.run_gui(exercise)

    def test_external_edit_is_not_overwritten(self):
        def exercise(window, path, errors):
            sp = window.pages["sources"]
            sp._edit(1)
            document = cfg.load_config(str(path))
            document["sources"][1]["model"] = "new-model-from-other-window"
            cfg.save_config(document, str(path))
            original = path.read_bytes()
            sp._save_form()
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(errors)
        self.run_gui(exercise)

    def test_stale_card_delete_cannot_delete_a_different_source(self):
        def exercise(window, path, errors):
            sp = window.pages["sources"]
            document = cfg.load_config(str(path))
            del document["sources"][0]
            cfg.save_config(document, str(path))
            original = path.read_bytes()
            sp._remove(1)  # Old card B now has C's index on disk.
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(errors)
        self.run_gui(exercise)

    def test_invalid_config_toggle_and_add_preserve_original_bytes(self):
        def exercise(window, path, errors):
            original = b'{"sources": broken'
            path.write_bytes(original)
            window.set_failover(False)
            self.assertEqual(path.read_bytes(), original)
            sp = window.pages["sources"]
            sp.open_add()
            for key, value in source("D").items():
                sp.vars[key].set(value)
            sp._save_form()
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(errors)
        self.run_gui(exercise)


class LegacyGuiSafetyTests(unittest.TestCase):
    def run_gui(self, action):
        import tkinter as tk
        from tkinter import messagebox
        setup = app.load_engine_module("setup.py", "_legacy_setup_test")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            cfg.save_config({"sources": [source(n) for n in ("A", "B", "C")]}, str(path))
            errors = []
            def mainloop(window):
                try:
                    window.update()
                    action(window, path, errors)
                finally:
                    window.destroy()
            with mock.patch.dict(os.environ, {cfg.CONFIG_ENV: str(path)}), \
                 mock.patch.object(tk.Tk, "mainloop", mainloop), \
                 mock.patch.object(messagebox, "showerror", side_effect=lambda *a, **k: errors.append(a)):
                setup.cmd_gui(None)

    def test_edit_after_external_delete_preserves_other_sources(self):
        def action(window, path, errors):
            window.tree.selection_set("1")
            window._on_select()
            window.vars["name"].set("B-renamed")
            current = cfg.load_config(str(path))
            del current["sources"][0]
            cfg.save_config(current, str(path))
            window._update()
            self.assertEqual([s["name"] for s in cfg.load_config(str(path))["sources"]], ["B-renamed", "C"])
        self.run_gui(action)

    def test_invalid_config_add_reports_error_and_preserves_bytes(self):
        def action(window, path, errors):
            for key, value in source("D").items():
                window.vars[key].set(value)
            original = b'{"sources": broken'
            path.write_bytes(original)
            window._add()
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(errors)
        self.run_gui(action)


class AgentSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.user_dir = Path(self.temp.name)
        for host in (".codex", ".claude", ".cursor"):
            (self.user_dir / host).mkdir()
        self.enterContext(mock.patch.object(app, "home", return_value=str(self.user_dir)))
        self.enterContext(mock.patch.object(app, "appdata_dir", return_value=str(self.user_dir / "config")))

    def test_codex_disable_keeps_shared_skill_and_uses_native_rule(self):
        config = self.user_dir / ".codex/config.toml"
        original = '# user comment\nmodel = "user-model"\n'
        config.write_text(original)
        app.install_skill([".agents", ".codex"])
        app.remove_skill([".codex"])
        import tomllib
        text = config.read_text()
        document = tomllib.loads(text)
        self.assertTrue(text.startswith(original))
        rules = document.get("skills", {}).get("config", [])
        self.assertTrue(any(r.get("name") == app.SKILL_NAME and r.get("enabled") is False for r in rules))
        self.assertTrue(app.host_hooked(".agents"))
        self.assertFalse(app.host_hooked(".codex"))
        app.install_skill([".codex"])
        self.assertTrue(app.host_hooked(".codex"))

    def test_invalid_codex_config_aborts_before_removing_skill(self):
        app.install_skill([".codex"])
        path = self.user_dir / ".codex/config.toml"
        path.write_text("bad = [")
        with self.assertRaises(RuntimeError):
            app.remove_skill([".codex"])
        self.assertTrue((self.user_dir / ".codex/skills" / app.SKILL_NAME / "SKILL.md").exists())
        self.assertEqual(path.read_text(), "bad = [")

    def test_cursor_shared_conflict_does_not_delete_other_agents(self):
        app.install_skill([".claude", ".cursor"])
        with self.assertRaisesRegex(RuntimeError, "Cursor|共享"):
            app.remove_skill([".cursor"])
        self.assertTrue(app.host_hooked(".claude"))

    def test_codex_policy_survives_windows_line_endings(self):
        app.install_skill([".codex"])
        path = self.user_dir / ".codex/config.toml"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        app.remove_skill([".codex"])
        self.assertEqual(path.read_bytes().count(b"# BEGIN IMG SWITCH SKILL POLICY"), 1)
        app.install_skill([".codex"])
        self.assertTrue(app.host_hooked(".codex"))

    def test_install_does_not_reconnect_excluded_cursor(self):
        app.install_skill([".cursor"])
        app.remove_skill([".cursor"])
        app.set_host_enabled(".cursor", False)
        with self.assertRaisesRegex(RuntimeError, "Cursor"):
            app.install_skill([".claude"])
        self.assertFalse(app.host_hooked(".cursor"))
        self.assertFalse(Path(app.skill_dst(".claude")).exists())

    def test_global_disconnect_without_cursor_preserves_foreign_skill(self):
        (self.user_dir / ".cursor").rmdir()
        foreign = Path(app.skill_dst(".claude"))
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("user managed skill")
        app.install_skill([".codex"])
        app.remove_skill()
        self.assertFalse(Path(app.skill_dst(".codex")).exists())
        self.assertEqual((foreign / "SKILL.md").read_text(), "user managed skill")

    def test_stale_exclusion_of_uninstalled_cursor_does_not_block_install(self):
        (self.user_dir / ".cursor").rmdir()
        app.set_host_enabled(".cursor", False)
        app.install_skill([".claude"])
        self.assertTrue(app.host_hooked(".claude"))


class RelayHandler(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *args):
        pass
    def send_data(self, data, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        self.send_data(b"<html>sign in</html>", "text/html")
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        mode = self.path.split("/")[1]
        self.requests.append(mode)
        png = make_png(64, 64)
        payloads = {
            "bad64": {"data": [{"b64_json": "abcde"}]},
            "array": {"images": ["data:image/png;base64," + base64.b64encode(png).decode()]},
            "truncated": {"data": [{"b64_json": base64.b64encode(png[:24]).decode()}]},
            "badurl": {"data": [{"url": 123}]},
            "good": {"data": [{"b64_json": base64.b64encode(png).decode()}]},
        }
        self.send_data(json.dumps(payloads[mode]).encode())


class RelaySafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_port
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_invalid_responses_reach_backup(self):
        for mode in ("bad64", "truncated", "badurl"):
            with self.subTest(mode=mode):
                RelayHandler.requests = []
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result, attempts = engine._run_attempts([
                            source("broken", self.base + "/" + mode + "/v1"),
                            source("backup", self.base + "/good/v1")], "audit", (64, 64), None, None)
                except Exception as error:
                    self.fail("Malformed relay response escaped failover: " + type(error).__name__)
                self.assertTrue(result)
                self.assertEqual(RelayHandler.requests, [mode, "good"])
                self.assertEqual(len(attempts), 2)

    def test_images_array_is_used_without_second_generation(self):
        result = None
        try:
            result = engine.attempt_source(source("array", self.base + "/array/v1"), "audit", (64, 64), None, "image")
        except engine.AttemptError:
            pass
        self.assertIsNotNone(result, "Valid images[] response was rejected")

    def test_html_is_not_a_healthy_model_list(self):
        self.assertFalse(app.probe_relay(self.base + "/v1", {})[2])

    def test_png_integrity(self):
        image = make_png(64, 64)
        self.assertEqual(engine.detect_image(image), ("png", 64, 64))
        for damaged in (image[:24], image[:-12], image[:-20] + b"corrupt" + image[-13:]):
            with self.subTest(length=len(damaged)):
                self.assertIsNone(engine.detect_image(damaged))

    def test_jpeg_headers_without_decoding_tables_are_rejected(self):
        fake = bytes.fromhex("ffd8ffc0000b080040004001011100ffda0008010100003f0000ffd9")
        self.assertIsNone(engine.detect_image(fake))

    def test_valid_jpeg_and_truncated_jpeg(self):
        jpeg = (Path(__file__).parent / "test-fixtures/valid-64.jpg").read_bytes()
        self.assertEqual(engine.detect_image(jpeg), ("jpg", 64, 64))
        self.assertIsNone(engine.detect_image(jpeg[:-2]))

    def test_batch_continues_after_malformed_response(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            path = work / "config.json"
            cfg.save_config({"sources": [source("bad", self.base + "/bad64/v1"),
                                          source("backup", self.base + "/good/v1")]}, str(path))
            (work / "jobs.txt").write_text("first\nsecond\n")
            env = dict(os.environ, FAILOVER_IMAGEGEN_TOOL_CONFIG=str(path), PYTHONDONTWRITEBYTECODE="1")
            result = subprocess.run([sys.executable, "-B", engine.__file__, "batch", "--prompts-file", "jobs.txt", "--size", "64x64"],
                                    cwd=work, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("成功 2 / 失败 0", result.stdout)
            self.assertEqual(len(list(work.glob("generated/*.png"))), 2)


if __name__ == "__main__":
    unittest.main()
