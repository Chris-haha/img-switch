"""Native Tk workflows; isolated configuration and local relay only."""
import importlib.util
import inspect
import base64
import json
import os
import subprocess
from pathlib import Path
import tempfile
import time
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from types import SimpleNamespace

import app
from engine.tests.mock_relay import make_png


class GUIRelay(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(data)
        self.server.received.set()
        time.sleep(self.server.delay)
        code = 400 if self.server.fail else 200
        payload = {"error": {"message": "invalid request"}} if self.server.fail else {
            "data": [{"b64_json": base64.b64encode(make_png(64, 64)).decode()}]}
        body = json.dumps(payload).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        body = json.dumps(self.server.models).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class WorkflowContractTests(unittest.TestCase):
    def test_gui_fixture_isolates_windows_appdata_and_inherited_jobs(self):
        with tempfile.TemporaryDirectory(prefix="gui-outside-fixture-") as directory:
            outside = Path(directory)
            inherited_config = outside / "appdata" / "failover-imagegen-tool" / "config.json"
            inherited_config.parent.mkdir(parents=True)
            original = json.dumps({"sources": [], "outside_fixture": True}).encode()
            inherited_config.write_bytes(original)
            windows_os = SimpleNamespace(**vars(os))
            windows_os.name = "nt"
            with mock.patch.dict(os.environ, {"APPDATA": str(outside / "appdata"),
                                              "LOCALAPPDATA": str(outside / "local"),
                                              "FAILOVER_IMAGEGEN_JOBS_DIR": str(outside / "jobs")}), \
                    mock.patch.object(app, "os", windows_os):
                for index in range(2):
                    case = NativeWorkflowTests()
                    try:
                        case.setUp()
                        root = Path(case.temp.name).resolve()
                        self.assertTrue(Path(app.appdata_config()).resolve().is_relative_to(root),
                                        "Windows AppData escaped the current test fixture")
                        self.assertTrue(Path(case.gui.tasks.jobs_dir).resolve().is_relative_to(root),
                                        "Inherited jobs directory escaped the current test fixture")
                        self.assertEqual(case.gui.tasks.records(), [], "a previous fixture leaked task history")
                        case.seed("succeeded", prompt="fixture %d" % index)
                    finally:
                        case.tearDown()
            self.assertEqual(inherited_config.read_bytes(), original)
            self.assertFalse((outside / "jobs").exists())

    def test_destroyed_gui_can_be_collected_by_worker_without_tcl_access(self):
        script = r'''
import gc, threading, sys
from test_reliability import GuiSafetyTests
case = GuiSafetyTests("test_delete_before_edited_source_does_not_overwrite_next_source")
case.debug()
del case
worker = threading.Thread(target=gc.collect)
worker.start()
worker.join(8)
assert not worker.is_alive(), "destroyed Tk objects blocked worker garbage collection"
print("worker collection clean")
'''
        result = subprocess.run([__import__("sys").executable, "-c", script],
                                cwd=Path(__file__).parent, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)
        self.assertIn("worker collection clean", result.stdout)

    def test_closed_root_variables_dialogs_and_timers_are_safe_to_release_in_worker(self):
        script = r'''
import gc, threading, time
import tkinter as tk
from tkinter import font as tkfont, ttk
from test_gui_workflows import NativeWorkflowTests
gc.disable()
case = NativeWorkflowTests()
case.setUp()
from tk_lifecycle import ManagedTk
root = case.gui
dialog = root.open_test_image(case.source["name"])
variable = root.status
font = tkfont.Font(root=root, family="Helvetica", size=10)
image = tk.PhotoImage(master=root, width=1, height=1)
style = ttk.Style(root)
held = [root, dialog, variable, root.pages["sources"], font, image, style]
root.after(40, lambda: print("ERROR: callback ran after close"))
root.destroy()
assert not root.winfo_exists()
assert not dialog.winfo_exists()
case.gui = None
case.tearDown()
del root, dialog, variable, font, image, style, case
def collect():
    held.clear()
    gc.collect()
worker = threading.Thread(target=collect)
worker.start()
worker.join(5)
assert not worker.is_alive()
# A fresh event loop must not run dangling callbacks from the old interpreter.
next_root = ManagedTk()
next_root.after(750, next_root.quit)
next_root.mainloop()
next_root.destroy()
print("external references and timers clean")
'''
        result = subprocess.run([__import__("sys").executable, "-c", script],
                                cwd=Path(__file__).parent, capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("Exception ignored", result.stderr)
        self.assertNotIn("invalid command name", result.stderr)
        self.assertNotIn("ERROR:", result.stdout)
        self.assertIn("external references and timers clean", result.stdout)

    def test_gui_can_be_embedded_without_starting_event_loop(self):
        self.assertIn("run_loop", inspect.signature(app.cmd_gui).parameters)

    def test_history_filters_and_pages_preserve_task_identity(self):
        self.assertIsNotNone(importlib.util.find_spec("gui_tasks"), "task workflow module missing")
        from gui_tasks import filter_tasks, page_tasks, retry_policy
        records = [{"id": str(i), "name": "同名任务", "status": "failed" if i % 2 else "succeeded",
                    "sources": ["local"], "items": []} for i in range(12)]
        filtered = filter_tasks(records, "local", {"failed"})
        page, number, pages = page_tasks(filtered, 2, 5)
        self.assertEqual([r["id"] for r in page], ["11"])
        self.assertEqual((number, pages), (2, 2))
        self.assertEqual(retry_policy({"status": "succeeded"}), "blocked")
        self.assertEqual(retry_policy({"status": "unknown"}), "confirm")
        self.assertEqual(retry_policy({"status": "failed"}), "retry")

    def test_source_edit_retains_capabilities_and_extra_options(self):
        self.assertIsNotNone(importlib.util.find_spec("gui_tasks"), "task workflow module missing")
        from gui_tasks import merge_source_edit
        old = {"name": "A", "models": ["old"], "timeout": 20,
               "supported_sizes": ["1024x1024"], "size_strategy": "omit",
               "provider_option": {"opaque": True}}
        new = {"name": "B", "model": "new", "base_url": "http://localhost/v1", "api_key": "test"}
        merged = merge_source_edit(old, new)
        self.assertEqual(merged["provider_option"], {"opaque": True})
        self.assertEqual(merged["supported_sizes"], ["1024x1024"])
        self.assertEqual(merged["size_strategy"], "omit")
        self.assertNotIn("models", merged)
        self.assertNotIn("timeout", merged)

    def test_corrupt_history_record_explains_read_failure(self):
        from gui_tasks import task_summary, retry_policy
        self.assertIn("记录损坏", task_summary({"status": "failed", "items": [],
                                               "error": "任务记录损坏，原文件已保留"}))
        self.assertEqual(retry_policy({"status": "failed", "items": [], "error": "损坏"}), "blocked")

    def test_windows_close_fallback_stops_owned_engine_process_tree(self):
        from gui_tasks import TaskController
        controller = TaskController(None, ["engine"], {}, lambda: None, "unused")
        process = mock.Mock()
        process.pid = 43210
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("engine", 3), 0]
        controller.processes["owned-task"] = process
        with mock.patch.object(controller, "cancel", return_value=mock.Mock()), \
                mock.patch("gui_tasks.os.name", "nt"), \
                mock.patch("gui_tasks.subprocess.run") as run:
            controller.close()
        self.assertTrue(run.called, "Windows close must stop the engine's whole process tree")
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "43210", "/T", "/F"])
        process.terminate.assert_not_called()


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="img-switch-gui-")
        self.addCleanup(self.temp.cleanup)
        workspace = Path(self.temp.name)
        config_home = workspace / ".config"
        self.gui = None
        self.env = mock.patch.dict(os.environ, {
            "FAILOVER_IMAGEGEN_APP_HOME": self.temp.name,
            "HOME": self.temp.name,
            "USERPROFILE": self.temp.name,
            "APPDATA": str(config_home),
            "LOCALAPPDATA": str(workspace / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(config_home),
            "FAILOVER_IMAGEGEN_TOOL_CONFIG": str(config_home / "failover-imagegen-tool" / "config.json"),
            "FAILOVER_IMAGEGEN_JOBS_DIR": str(workspace / "jobs"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self._close_gui)
        config = Path(app.appdata_config())
        # Verify isolation before touching any config, on every host platform.
        self.assertTrue(config.resolve().is_relative_to(workspace.resolve()))
        self.assertEqual(str(config), app.engine_env()["FAILOVER_IMAGEGEN_TOOL_CONFIG"])
        config.parent.mkdir(parents=True, exist_ok=True)
        self.source = {"name": "测试源名称特别长" * 6, "base_url": "http://127.0.0.1:9/v1",
                       "api_key": "test-secret", "models": ["gpt-image-2", "模型名称特别长" * 8],
                       "supported_sizes": ["1024x1024"], "size_strategy": "omit"}
        config.write_text(json.dumps({"version": 1, "sources": [self.source]}), encoding="utf-8")
        self.gui = app.cmd_gui(run_loop=False)
        self.assertTrue(Path(self.gui.tasks.jobs_dir).resolve().is_relative_to(workspace.resolve()))
        self.assertEqual(self.gui.tasks.records(), [], "task history leaked from another test")
        self.gui.geometry("880x640+0+0")
        self.gui.update()
        # Synthetic keyboard events are discarded by Tk when another desktop
        # application owns focus. Exercise keys with this test window active.
        self.gui.focus_force()
        self.gui.update()

    def relay(self, delay=0.2, fail=False):
        server = ThreadingHTTPServer(("127.0.0.1", 0), GUIRelay)
        server.requests, server.delay, server.fail = [], delay, fail
        server.models = {"data": [{"id": "gpt-image-2"}]}
        server.received = threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.source["base_url"] = "http://127.0.0.1:%d/v1" % server.server_port
        Path(app.appdata_config()).write_text(json.dumps({"sources": [self.source]}), encoding="utf-8")
        return server

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.gui.update()
            if predicate():
                return
            time.sleep(0.015)
        self.fail("timed out waiting for GUI state")

    def seed(self, state, prompt="同名任务", created="2026-11-27T12:00:01+00:00"):
        store = self.gui.task_store()
        return store.create_task(self.gui.tasks.jobs_dir, {
            "id": str(uuid.uuid4()), "status": state, "name": "seed", "kind": "generate",
            "sources": [self.source["name"]], "created_at": created,
            "items": [{"id": "1", "status": state, "prompt": prompt}], "size": [1024, 1024],
            "options": {"model": "gpt-image-2"}})

    def _close_gui(self):
        if getattr(self, "gui", None):
            if hasattr(self.gui, "tasks"):
                self.gui.tasks.close()
            self.gui.destroy()
            self.gui = None

    def tearDown(self):
        # Also used directly by isolated lifecycle/screenshot scenarios.
        self.doCleanups()

    def test_history_navigation_and_test_dialog_work_at_minimum_size(self):
        self.assertIn("history", self.gui.pages, "persistent task history page missing")
        self.assertTrue(hasattr(self.gui, "open_test_image"), "test image dialog missing")
        self.gui.show_page("history")
        self.gui.update()
        dialog = self.gui.open_test_image(self.source["name"])
        self.gui.update()
        self.assertEqual(dialog.size_var.get(), "1024x1024")
        self.assertIn("费用", dialog.cost_label.cget("text"))
        self.assertTrue(dialog.generate_button.winfo_ismapped())
        self.assertTrue(dialog.cancel_button.winfo_ismapped())
        self.assertLessEqual(dialog.generate_button.winfo_rootx() + dialog.generate_button.winfo_width(),
                             dialog.winfo_rootx() + dialog.winfo_width())
        dialog.close()

    def test_source_operations_remain_visible_with_long_models_and_edit_keeps_capabilities(self):
        self.gui.show_page("sources")
        self.gui.update()
        page = self.gui.pages["sources"]
        labels = []
        def visit(widget):
            if widget.winfo_class() in ("Label", "TButton"):
                labels.append(widget)
            for child in widget.winfo_children():
                visit(child)
        visit(page._card_refs[0])
        for caption in ("编辑", "删除", "测试出图", "模型详情"):
            matches = [widget for widget in labels if widget.cget("text") == caption]
            self.assertTrue(matches, "missing action: " + caption)
            widget = matches[0]
            self.assertGreater(widget.winfo_width(), 15)
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                 self.gui.winfo_rootx() + self.gui.winfo_width())
        page._edit(0)
        page.vars["name"].set("edited")
        page._save_form()
        saved = self.gui.read_sources()[0]
        self.assertEqual(saved["supported_sizes"], ["1024x1024"])
        self.assertEqual(saved["size_strategy"], "omit")

    def test_real_generation_remains_responsive_and_displays_dimensions(self):
        server = self.relay(delay=0.35)
        dialog = self.gui.open_test_image(self.source["name"])
        pulses = []
        def pulse():
            pulses.append(time.monotonic())
            if dialog.winfo_exists():
                self.gui.after(20, pulse)
        self.gui.after(20, pulse)
        dialog.generate_button.invoke()
        self.wait_for(lambda: dialog.task_id and dialog.task_id not in self.gui.tasks.active_ids())
        dialog._poll()
        self.assertGreater(len(pulses), 5, "Tk event loop stalled during network request")
        record = self.gui.tasks.get(dialog.task_id)
        self.assertEqual(record["status"], "succeeded", record)
        self.assertIn("实际尺寸 64×64", dialog.status_var.get())
        self.assertIn("耗时", dialog.status_var.get())
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["size"], "1024x1024")
        self.assertTrue(Path(record["items"][0]["output"]["image"]).is_file())
        self.assertEqual(str(dialog.image_button.cget("state")), "normal")
        dialog.close()
        self.gui.show_page("history")
        self.gui.update()
        self.assertIn(record["id"], self.gui.pages["history"].row_buttons)
        with self.assertRaisesRegex(ValueError, "不可恢复"):
            self.gui.tasks.retry(record["id"])

    def test_dialog_close_keeps_task_tracked_and_cancel_records_unknown(self):
        server = self.relay(delay=2)
        dialog = self.gui.open_test_image(self.source["name"])
        dialog.generate()
        self.wait_for(server.received.is_set)
        task_id = dialog.task_id
        dialog.close()
        self.assertIn(task_id, self.gui.tasks.active_ids())
        self.gui.tasks.cancel(task_id)
        self.wait_for(lambda: task_id not in self.gui.tasks.active_ids())
        task = self.gui.tasks.get(task_id)
        self.assertEqual(task["status"], "unknown", task)
        self.assertTrue(task["cancel_requested"])
        with mock.patch("gui_tasks.messagebox.askyesno", return_value=False) as confirm:
            self.gui.pages["history"].retry(task_id)
        confirm.assert_called_once()
        self.assertEqual(len(server.requests), 1, "unknown cancellation must not retry automatically")

    def test_history_multiselect_pagination_and_menu_use_stable_ids(self):
        records = [self.seed("failed" if i % 2 else "succeeded",
                             created="2026-11-27T12:00:%02d+00:00" % i) for i in range(12)]
        page = self.gui.pages["history"]
        self.gui.show_page("history")
        self.gui.update()
        page.change_page(1)
        self.assertEqual(page.page, 2)
        page.statuses.update({"failed", "succeeded"})
        page.filter_changed()
        self.assertEqual(page.page, 1)
        self.assertIn("12", page.total_label.cget("text"))
        page.statuses.remove("succeeded")
        page.filter_changed()
        self.assertIn("6", page.total_label.cget("text"))
        self.gui.update()
        target = records[-1]["id"]
        page.show_actions(target, page.row_buttons[target])
        self.gui.update()
        self.assertEqual(page.selected_id, target)
        self.assertEqual(page.popup.winfo_width(), 192)
        page.popup.event_generate("<Escape>")
        self.gui.update()
        self.assertFalse(page.popup.winfo_exists())
        page.show_status_filter()
        self.gui.update()
        self.assertEqual(page.popup.winfo_width(), 236)
        page._dismiss_popup()
        page.query.set("no-such-prompt")
        page.filter_changed()
        self.assertFalse(page.row_buttons)
        self.assertIn("0", page.total_label.cget("text"))

    def test_minimum_history_wraps_source_and_marks_truncated_summaries(self):
        record = self.seed("unknown", prompt="长标题" * 100)
        record["sources"] = [self.source["name"] * 2]
        self.gui.task_store().save_task(self.gui.tasks.jobs_dir, record)
        page = self.gui.pages["history"]
        self.gui.show_page("history")
        self.gui.update()
        row = page.scroll.inner.winfo_children()[0]
        info = row.grid_slaves(row=0, column=1)[0]
        title, summary, source = info.winfo_children()
        self.assertTrue(source.cget("text").endswith("…"))
        self.assertTrue(title.cget("text").endswith("…"))
        self.assertGreater(int(source.cget("wraplength")), 0)
        self.assertLessEqual(int(summary.cget("wraplength")), info.winfo_width() - 12)
        self.assertIn("再决定重试", summary.cget("text"))
        menu = page.row_buttons[record["id"]]
        self.assertGreater(menu.winfo_width(), 15)
        self.assertLessEqual(menu.winfo_rootx() + menu.winfo_width(), self.gui.winfo_rootx() + self.gui.winfo_width())

    def test_app_close_cancels_inflight_request_and_does_not_orphan_task(self):
        server = self.relay(delay=3)
        dialog = self.gui.open_test_image(self.source["name"])
        dialog.generate()
        self.wait_for(server.received.is_set)
        task_id = dialog.task_id
        self.gui.tasks.close()
        self.assertFalse(self.gui.tasks.active_ids())
        task = self.gui.tasks.get(task_id)
        self.assertEqual(task["status"], "unknown")
        self.assertTrue(task["cancel_requested"])
        self.assertEqual(len(server.requests), 1)

    def test_failed_task_recovers_and_unknown_retry_requires_affirmative_confirmation(self):
        server = self.relay(delay=0.05, fail=True)
        dialog = self.gui.open_test_image(self.source["name"])
        dialog.generate()
        self.wait_for(lambda: dialog.task_id not in self.gui.tasks.active_ids())
        task_id = dialog.task_id
        self.assertEqual(self.gui.tasks.get(task_id)["status"], "failed")
        server.fail = False
        self.gui.pages["history"].retry(task_id)
        self.wait_for(lambda: task_id not in self.gui.tasks.active_ids())
        self.assertEqual(self.gui.tasks.get(task_id)["status"], "succeeded")
        self.assertEqual(len(server.requests), 2)
        server.delay = 2
        dialog.generate()
        self.wait_for(lambda: len(server.requests) == 3)
        task_id = dialog.task_id
        dialog.cancel()
        self.wait_for(lambda: task_id not in self.gui.tasks.active_ids())
        self.assertEqual(self.gui.tasks.get(task_id)["status"], "unknown")
        server.delay = 0.01
        with mock.patch("gui_tasks.messagebox.askyesno", return_value=True) as confirmation:
            self.gui.pages["history"].retry(task_id)
        confirmation.assert_called_once()
        self.wait_for(lambda: task_id not in self.gui.tasks.active_ids())
        self.assertEqual(self.gui.tasks.get(task_id)["status"], "succeeded")
        self.assertEqual(len(server.requests), 4)
        dialog.close()

    def test_model_fetch_redacts_secrets_and_rejects_malformed_response(self):
        server = self.relay()
        server.models = {"data": [{"id": "gpt-image-test-secret"}]}
        page = self.gui.pages["sources"]
        page.open_add()
        page.vars["base_url"].set(self.source["base_url"] + "?token=test-secret")
        page.vars["api_key"].set("test-secret")
        page._fetch_models()
        self.assertNotIn("test-secret", self.gui.status.get())
        self.wait_for(lambda: page.fetch_btn.cget("text") != "获取中…")
        self.assertNotIn("test-secret", repr(page.model_box.cget("values")))
        server.models = {"data": [{"id": 123}]}
        page._fetch_models()
        self.wait_for(lambda: page.fetch_btn.cget("text") != "获取中…")
        self.assertIn("获取失败", self.gui.status.get())

    def test_cancel_during_engine_startup_is_not_lost(self):
        server = self.relay(delay=2)
        import sys
        self.gui.tasks.command = [sys.executable, "-c",
            "import runpy,sys,time;time.sleep(.35 if 'generate' in sys.argv else 0);sys.argv=[sys.argv[1]]+sys.argv[2:];runpy.run_path(sys.argv[0],run_name='__main__')",
            str(Path(app.__file__)), "__tool"]
        dialog = self.gui.open_test_image(self.source["name"])
        dialog.generate()
        dialog.cancel()
        self.wait_for(lambda: dialog.task_id not in self.gui.tasks.active_ids())
        record = self.gui.tasks.get(dialog.task_id)
        self.assertTrue(record["cancel_requested"], record)
        self.assertIn(record["status"], ("cancelled", "unknown"))
        dialog.close()

    def test_cancel_waits_through_real_journal_write_lock(self):
        server = self.relay(delay=5)
        dialog = self.gui.open_test_image(self.source["name"])
        dialog.generate()
        self.wait_for(server.received.is_set)
        path = str(Path(self.gui.tasks.jobs_dir) / (dialog.task_id + ".json"))
        with self.gui.task_store().failovercfg.config_lock(path):
            dialog.cancel()
            until = time.monotonic() + 0.45
            while time.monotonic() < until:
                self.gui.update()
                time.sleep(.01)
        self.wait_for(lambda: self.gui.tasks.get(dialog.task_id)["cancel_requested"], timeout=3)
        self.wait_for(lambda: dialog.task_id not in self.gui.tasks.active_ids())
        self.assertEqual(self.gui.tasks.get(dialog.task_id)["status"], "unknown")
        self.assertEqual(len(server.requests), 1)
        dialog.close()

    def test_cancel_lock_timeout_is_visible_and_button_allows_retry(self):
        server = self.relay(delay=8)
        dialog = self.gui.open_test_image(self.source["name"])
        dialog.generate()
        self.wait_for(server.received.is_set)
        path = str(Path(self.gui.tasks.jobs_dir) / (dialog.task_id + ".json"))
        with self.gui.task_store().failovercfg.config_lock(path):
            dialog.cancel()
            thread = self.gui.tasks._cancel_threads[dialog.task_id]
            self.wait_for(lambda: not thread.is_alive(), timeout=5)
            dialog._poll()
            self.assertFalse(self.gui.tasks.get(dialog.task_id)["cancel_requested"])
            self.assertIn("取消失败", dialog.status_var.get())
            self.assertEqual(str(dialog.cancel_button.cget("state")), "normal")
        dialog.cancel_button.invoke()
        self.wait_for(lambda: dialog.task_id not in self.gui.tasks.active_ids())
        self.assertTrue(self.gui.tasks.get(dialog.task_id)["cancel_requested"])
        self.assertEqual(len(server.requests), 1)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
