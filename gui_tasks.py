"""Native desktop task workflows, backed by the engine's persistent job journal."""
import copy
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
import uuid


STATUS = {
    "queued": ("◷", "等待开始", "#9a6700"),
    "running": ("●", "生成中", "#0969da"),
    "succeeded": ("✓", "已完成", "#1a7f37"),
    "failed": ("×", "失败", "#cf222e"),
    "unknown": ("?", "结果待核实", "#9a6700"),
    "cancelled": ("⊘", "已取消", "#59636e"),
    "partial": ("◐", "部分完成", "#9a6700"),
}
BG, LINE, TEXT, MUTED = "#ffffff", "#d1d9e0", "#1f2328", "#59636e"
FONT = ("Microsoft YaHei UI", 10)
SMALL = ("Microsoft YaHei UI", 9)


def merge_source_edit(original, edited):
    result = copy.deepcopy(original or {})
    for key in ("name", "base_url", "api_key", "model", "models", "enabled", "timeout"):
        result.pop(key, None)
    result.update(edited)
    return result


def filter_tasks(records, query="", statuses=()):
    query = query.strip().casefold()
    return [task for task in records
            if (not statuses or task.get("status") in statuses)
            and (not query or query in " ".join([
                str(task.get("id", "")), str(task.get("name", "")),
                " ".join(task.get("sources", [])),
                " ".join(str(item.get("prompt", "")) for item in task.get("items", []))
            ]).casefold())]


def page_tasks(records, page, per_page):
    per_page = max(1, int(per_page))
    pages = max(1, (len(records) + per_page - 1) // per_page)
    page = max(1, min(int(page), pages))
    return records[(page - 1) * per_page:page * per_page], page, pages


def retry_policy(task):
    if task.get("error") and not task.get("items"):
        return "blocked"
    statuses = {task.get("status")} | {item.get("status") for item in task.get("items", [])}
    if "running" in statuses:
        return "blocked"
    if "unknown" in statuses:
        return "confirm"
    if statuses & {"failed", "queued", "cancelled"}:
        return "retry"
    return "blocked"


def task_summary(task):
    counts = task.get("counts", {})
    items = task.get("items", [])
    total = counts.get("total", len(items))
    done = counts.get("succeeded", sum(i.get("status") == "succeeded" for i in items))
    state = STATUS.get(task.get("status"), ("?", "未知状态", MUTED))[1]
    error = next((str(i.get("error")) for i in items if i.get("error")), str(task.get("error") or ""))
    if task.get("status") == "unknown":
        error = "服务端可能已处理；请先核对计费或结果，再决定重试"
    if task.get("cancel_requested") and task.get("status") == "running":
        error = "已请求取消；等待在途请求结束并记录结果"
    return "%s · 完成 %d/%d 张%s" % (state, done, total, " · " + error if error else "")


def result_files(task):
    return [item["output"]["image"] for item in task.get("items", [])
            if isinstance(item.get("output"), dict) and item["output"].get("image")]


def readable_time(value):
    if not value:
        return "—"
    try:
        if isinstance(value, (float, int)):
            value = datetime.datetime.fromtimestamp(value).isoformat()
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return str(value)[:19]


def elapsed_text(task):
    duration = task.get("duration_seconds")
    if duration is None and task.get("started_at"):
        try:
            start = datetime.datetime.fromisoformat(str(task["started_at"]).replace("Z", "+00:00"))
            end = datetime.datetime.fromisoformat(str(task["finished_at"]).replace("Z", "+00:00")) if task.get("finished_at") else datetime.datetime.now(start.tzinfo)
            duration = (end - start).total_seconds()
        except (TypeError, ValueError):
            pass
    return "耗时 %.1f 秒" % max(0, duration) if isinstance(duration, (int, float)) else "尚未开始"


def open_path(path):
    if not path or not os.path.exists(path):
        raise OSError("结果文件不存在，可能已被移动：" + str(path))
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


class TaskController:
    """Processes belong to the app, never to a transient dialog."""
    def __init__(self, app, command, env, store_loader, jobs_dir):
        # Background collectors must retain only ordinary Python data, never a
        # Tk app or a bound loader method that keeps its interpreter alive.
        self.command, self.env = command, env
        self.store, self.jobs_dir = store_loader(), str(jobs_dir)
        self.processes, self.logs = {}, {}
        self._cancel_threads = {}
        self._cancellations = {}
        self._lock = threading.Lock()
        self._closed = False

    def records(self):
        return self.store.list_tasks(self.jobs_dir)

    def get(self, task_id):
        return next((t for t in self.records() if t.get("id", t.get("task_id")) == task_id), None)

    def _launch(self, task_id, args):
        if self._closed:
            raise RuntimeError("应用正在关闭")
        if task_id in self.processes and self.processes[task_id].poll() is None:
            raise RuntimeError("此任务仍在运行，请等待结果")
        Path(self.jobs_dir).mkdir(parents=True, exist_ok=True)
        # Pipe output is drained outside Tk; only the journal is used as task truth.
        process = subprocess.Popen(self.command + args, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", env=self.env, bufsize=1)
        self.processes[task_id] = process
        self.logs[task_id] = "正在启动生图引擎…"
        self._cancellations.pop(task_id, None)

        def collect():
            for line in process.stdout:
                with self._lock:
                    self.logs[task_id] = (self.logs.get(task_id, "") + "\n" + line)[-12000:]
            process.wait()
            process.stdout.close()
        threading.Thread(target=collect, daemon=True).start()
        return task_id

    def generate(self, source, model, size, deadline, prompt):
        if not source or not model or not prompt.strip():
            raise ValueError("请选择中转源、模型并填写提示词")
        try:
            deadline = float(deadline)
        except (TypeError, ValueError):
            raise ValueError("总时限应为 1 到 3600 秒") from None
        if not 1 <= deadline <= 3600:
            raise ValueError("总时限应为 1 到 3600 秒")
        task_id = str(uuid.uuid4())
        outdir = Path(self.jobs_dir).parent / "generated"
        return self._launch(task_id, ["generate", "--jobs-dir", self.jobs_dir,
            "--task-id", task_id, "--source", source, "--model", model,
            "--size", size, "--deadline", str(deadline), "--prompt", prompt.strip(),
            "--name", "test-" + task_id[:8], "--outdir", str(outdir)])

    def retry(self, task_id, confirm_unknown=False):
        task = self.get(task_id)
        if not task:
            raise ValueError("任务不存在，请刷新历史记录")
        policy = retry_policy(task)
        if policy == "blocked":
            raise ValueError("此任务不可恢复；已完成的图片不会重复生成")
        if policy == "confirm" and not confirm_unknown:
            raise ValueError("结果待核实；重试可能重复计费，需要明确确认")
        args = ["retry", "--jobs-dir", self.jobs_dir, "--task-id", task_id]
        if confirm_unknown:
            args.append("--confirm-unknown")
        return self._launch(task_id, args)

    def cancel(self, task_id):
        # This is a local journal operation. The engine keeps ownership of cleanup.
        existing = self._cancel_threads.get(task_id)
        if existing is not None and existing.is_alive():
            return existing
        with self._lock:
            self._cancellations[task_id] = {"status": "pending", "error": ""}
        def request():
            # A click can arrive before generate has durably created its UUID.
            # Wait for that journal, never discard the user's cancel request.
            stop_at = time.monotonic() + 5
            path = Path(self.jobs_dir) / (task_id + ".json")
            process = self.processes.get(task_id)
            while not path.exists() and process is not None and process.poll() is None and time.monotonic() < stop_at:
                time.sleep(0.025)
            # Only the local cancellation write is retried. A normal short journal
            # write must not lose the user's cancellation or replay generation.
            lock_deadline = time.monotonic() + 2
            while True:
                try:
                    completed = subprocess.run(self.command + ["cancel", "--jobs-dir", self.jobs_dir,
                        "--task-id", task_id], env=self.env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=3)
                    error = completed.stdout.strip() if completed.returncode else ""
                except (OSError, subprocess.TimeoutExpired) as exception:
                    error = "无法写入取消请求：" + str(exception)
                remaining = lock_deadline - time.monotonic()
                if error and "配置正在被其他进程保存" in error and remaining > 0:
                    time.sleep(min(.08, remaining))
                    continue
                break
            with self._lock:
                self._cancellations[task_id] = {"status": "failed" if error else "requested", "error": error}
                if error:
                    self.logs[task_id] = error[-12000:]
        thread = threading.Thread(target=request, daemon=True)
        self._cancel_threads[task_id] = thread
        thread.start()
        return thread

    def cancellation(self, task_id):
        with self._lock:
            return dict(self._cancellations.get(task_id, {}))

    def active_ids(self):
        return [key for key, process in self.processes.items() if process.poll() is None]

    def close(self):
        """Request journal cancellation then terminate children on app shutdown."""
        self._closed = True
        active = self.active_ids()
        cancel_threads = [self.cancel(task_id) for task_id in active]
        stop_at = time.monotonic() + 8
        for thread in cancel_threads:
            thread.join(max(0, stop_at - time.monotonic()))
        for task_id in active:
            process = self.processes[task_id]
            try:
                # The engine owns a request worker and terminates it on cancel.
                # Give it time to persist unknown and reap its worker before exit.
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    # TerminateProcess does not deliver SIGTERM on Windows.
                    # Target only the engine PID launched and owned by this app.
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       timeout=3, check=True)
                    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
                        process.terminate()
                else:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


class DetailDialog(tk.Toplevel):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.title(title)
        self.geometry("700x500")
        self.minsize(540, 350)
        self.configure(bg=BG)
        self.transient(parent.winfo_toplevel())
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=16, pady=12)
        tk.Label(head, text=title, font=(FONT[0], 12, "bold"), bg=BG).pack(side="left")
        ttk.Button(head, text="关闭 ×", command=self.destroy).pack(side="right")
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        scroll = ttk.Scrollbar(body)
        scroll.pack(side="right", fill="y")
        self.text = tk.Text(body, wrap="word", font=FONT, relief="flat", padx=8,
                            yscrollcommand=scroll.set, bg=BG, fg=TEXT)
        self.text.pack(fill="both", expand=True)
        scroll.config(command=self.text.yview)
        self.set_text(text)
        self.bind("<Escape>", lambda _e: self.destroy())

    def set_text(self, value):
        self.text.config(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", value)
        self.text.config(state="disabled")


class ScrollRows(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.cv = tk.Canvas(self, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, command=self.cv.yview)
        self.cv.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.cv.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.cv, bg=BG)
        self.window = self.cv.create_window(0, 0, anchor="nw", window=self.inner)
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.window, width=e.width))
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))


class AnchoredPopover(tk.Toplevel):
    def __init__(self, parent, anchor, width):
        super().__init__(parent)
        self.withdraw()
        self.overrideredirect(True)
        self.transient(parent.winfo_toplevel())
        self.configure(bg=BG, highlightbackground=LINE, highlightthickness=1)
        self.anchor, self.fixed_width = anchor, width
        self.bind("<Escape>", lambda e: self.destroy())
        self._outside = parent.winfo_toplevel().bind("<Button-1>", self._click_outside, add="+")
        self.bind("<Destroy>", self._cleanup)

    def _cleanup(self, event):
        if event.widget is self and self._outside:
            self.master.winfo_toplevel().unbind("<Button-1>", self._outside)
            self._outside = None

    def _click_outside(self, event):
        if not str(event.widget).startswith(str(self)):
            self.destroy()

    def show(self, focus):
        self.update_idletasks()
        x = self.anchor.winfo_rootx() + self.anchor.winfo_width() - self.fixed_width
        y = self.anchor.winfo_rooty() + self.anchor.winfo_height() + 4
        x = max(8, min(x, self.winfo_screenwidth() - self.fixed_width - 8))
        y = max(8, min(y, self.winfo_screenheight() - self.winfo_reqheight() - 8))
        self.geometry("%dx%d+%d+%d" % (self.fixed_width, self.winfo_reqheight(), x, y))
        self.deiconify()
        self.lift()
        focus.focus_set()


class TaskHistoryPage(tk.Frame):
    def __init__(self, master, app):
        super().__init__(master, bg=BG, padx=20, pady=18)
        self.app, self.page, self.records_cache = app, 1, []
        self.selected_id, self.statuses, self._signature = None, set(), None
        self.popup = None
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", pady=(0, 8))
        tk.Label(header, text="任务历史", bg=BG, fg=TEXT, font=(FONT[0], 15, "bold")).pack(side="left")
        ttk.Button(header, text="测试出图", command=app.open_test_image).pack(side="right")
        tk.Label(self, text="每次生图独立保存；关闭测试窗口后可在这里跟踪。", bg=BG, fg=MUTED, font=SMALL, anchor="w").pack(fill="x")
        search = tk.Frame(self, bg=BG)
        search.pack(fill="x", pady=(12, 14))
        tk.Label(search, text="搜索", bg=BG, font=SMALL).pack(side="left", padx=(0, 8))
        self.query = tk.StringVar()
        self.search_entry = ttk.Entry(search, textvariable=self.query, font=FONT)
        self.search_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(search, text="查询", command=self.filter_changed).pack(side="left", padx=(6, 0))
        ttk.Button(search, text="重置", command=self.reset_filter).pack(side="left", padx=(6, 0))
        self.search_entry.bind("<Return>", lambda e: self.filter_changed())
        box = tk.Frame(self, bg=BG, highlightbackground=LINE, highlightthickness=1)
        box.pack(fill="both", expand=True)
        toolbar = tk.Frame(box, bg="#f6f8fa", padx=12, pady=8)
        toolbar.pack(fill="x")
        self.count_label = tk.Label(toolbar, bg="#f6f8fa", fg=MUTED, font=(FONT[0], 10, "bold"))
        self.count_label.pack(side="left")
        self.status_button = ttk.Button(toolbar, text="状态：全部", command=self.show_status_filter)
        self.status_button.pack(side="right")
        ttk.Button(toolbar, text="刷新", command=lambda: self.refresh(force=True)).pack(side="right", padx=6)
        self.scroll = ScrollRows(box)
        self.scroll.pack(fill="both", expand=True)
        pagination = tk.Frame(self, bg=BG)
        pagination.pack(pady=(12, 0))
        self.total_label = tk.Label(pagination, bg=BG, fg=MUTED, font=SMALL)
        self.total_label.pack(side="left", padx=4)
        self.previous = ttk.Button(pagination, text="上一页", command=lambda: self.change_page(-1))
        self.previous.pack(side="left", padx=3)
        self.page_label = tk.Label(pagination, bg=BG, font=SMALL)
        self.page_label.pack(side="left", padx=4)
        self.next = ttk.Button(pagination, text="下一页", command=lambda: self.change_page(1))
        self.next.pack(side="left", padx=3)
        tk.Label(pagination, text="每页", bg=BG, font=SMALL).pack(side="left", padx=(8, 4))
        self.per_page = tk.StringVar(value="10")
        size = ttk.Combobox(pagination, textvariable=self.per_page, values=("5", "10", "20"), width=3, state="readonly")
        size.pack(side="left")
        size.bind("<<ComboboxSelected>>", lambda e: self.filter_changed())
        self.after(500, self._poll)

    def _poll(self):
        if not self.winfo_exists():
            return
        if self.app._current_page is self:
            self.refresh()
        self.after(700, self._poll)

    def filter_changed(self):
        self.page = 1
        self.refresh(force=True)

    def reset_filter(self):
        self.query.set("")
        self.statuses.clear()
        self.filter_changed()

    def change_page(self, delta):
        self.page += delta
        self.refresh(force=True)

    def refresh(self, force=False):
        try:
            records = self.app.tasks.records()
        except Exception as error:
            self.count_label.config(text="历史读取失败：" + self.app.safe_text(str(error), 100))
            return
        self.records_cache = records
        filtered = filter_tasks(records, self.query.get(), self.statuses)
        shown, self.page, pages = page_tasks(filtered, self.page, self.per_page.get())
        signature = (repr(shown), self.page, pages, len(filtered), tuple(sorted(self.statuses)),
                     repr([self.app.tasks.cancellation(task.get("id")) for task in shown]))
        if not force and signature == self._signature:
            return
        self._signature = signature
        self.count_label.config(text="%d 条运行记录" % len(filtered))
        self.total_label.config(text="共 %d 条" % len(filtered))
        self.page_label.config(text="%d / %d" % (self.page, pages))
        self.previous.config(state="normal" if self.page > 1 else "disabled")
        self.next.config(state="normal" if self.page < pages else "disabled")
        self.status_button.config(text="状态：%d 项" % len(self.statuses) if self.statuses else "状态：全部")
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self.row_buttons = {}
        for task in shown:
            self._row(task)
        if not shown:
            tk.Label(self.scroll.inner, text="没有符合条件的记录。可调整筛选条件或开始测试出图。", bg=BG, fg=MUTED, wraplength=450, pady=24).pack(fill="x")

    def _row(self, task):
        def excerpt(value, limit):
            clean = self.app.safe_text(value, limit + 1)
            return clean[:limit] + "…" if len(clean) > limit else clean

        task_id = task.get("id", task.get("task_id"))
        row = tk.Frame(self.scroll.inner, bg=BG, padx=12, pady=10)
        row.pack(fill="x")
        row.grid_columnconfigure(1, weight=1)
        icon, _label, color = STATUS.get(task.get("status"), ("?", "未知", MUTED))
        tk.Label(row, text=icon, fg=color, bg=BG, font=(FONT[0], 12, "bold")).grid(row=0, column=0, sticky="nw", padx=(0, 8))
        info = tk.Frame(row, bg=BG)
        info.grid(row=0, column=1, sticky="nsew")
        title = (task.get("items") or [{}])[0].get("prompt") or task.get("name") or task_id
        button = tk.Label(info, text=excerpt(title, 240), bg=BG, fg=TEXT, font=(FONT[0], 10, "bold"), anchor="w", justify="left", cursor="hand2", takefocus=True)
        button.pack(fill="x")
        button.bind("<Button-1>", lambda e, ident=task_id: self.details(ident))
        button.bind("<Return>", lambda e, ident=task_id: self.details(ident))
        description = task_summary(task)
        if task.get("status") == "unknown":
            description = description.replace(" · 服务端可能已处理", "\n服务端可能已处理", 1)
        cancellation = self.app.tasks.cancellation(task_id)
        if cancellation.get("status") == "failed":
            description += "\n取消失败，可再次取消：" + cancellation["error"]
        elif cancellation.get("status") == "pending":
            description += "\n正在提交取消请求…"
        summary = tk.Label(info, text=excerpt(description, 400), bg=BG, fg=MUTED, font=SMALL, anchor="w", justify="left")
        summary.pack(fill="x", pady=(3, 0))
        sources = "、".join(task.get("sources", [])) or "中转源未记录"
        source_label = tk.Label(info, text=excerpt(sources, 50), bg=BG, fg="#0550ae", font=SMALL, anchor="w", justify="left")
        source_label.pack(fill="x", pady=(2, 0))
        def fit(event):
            for label in (button, summary, source_label):
                label.config(wraplength=max(100, event.width - 12))
        info.bind("<Configure>", fit)
        timing = tk.Frame(row, bg=BG)
        timing.grid(row=0, column=2, sticky="ne", padx=(10, 8))
        tk.Label(timing, text=readable_time(task.get("created_at")), bg=BG, fg=MUTED, font=SMALL).pack(anchor="w")
        tk.Label(timing, text=elapsed_text(task), bg=BG, fg=MUTED, font=SMALL).pack(anchor="w", pady=(5, 0))
        more = ttk.Button(row, text="⋯", width=2)
        more.config(command=lambda ident=task_id, anchor=more: self.show_actions(ident, anchor))
        more.grid(row=0, column=3, sticky="ne")
        self.row_buttons[task_id] = more
        tk.Frame(self.scroll.inner, height=1, bg=LINE).pack(fill="x")

    def _dismiss_popup(self):
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.destroy()

    def show_status_filter(self):
        self._dismiss_popup()
        pop = self.popup = AnchoredPopover(self, self.status_button, 236)
        tk.Label(pop, text="筛选状态", bg=BG, font=(FONT[0], 10, "bold"), anchor="w").pack(fill="x", padx=12, pady=(12, 8))
        field = tk.Frame(pop, bg=BG)
        field.pack(fill="x", padx=10, pady=(0, 6))
        tk.Label(field, text="搜索", bg=BG, font=SMALL).pack(side="left", padx=(0, 6))
        query = tk.StringVar()
        entry = ttk.Entry(field, textvariable=query, width=12)
        entry.pack(fill="x", expand=True)
        choices = tk.Frame(pop, bg=BG)
        choices.pack(fill="x", padx=8, pady=(0, 10))
        def render(*_args):
            for child in choices.winfo_children():
                child.destroy()
            for state, (_icon, label, _color) in STATUS.items():
                if query.get().strip() not in label:
                    continue
                var = tk.BooleanVar(value=state in self.statuses)
                def toggle(s=state, v=var):
                    self.statuses.add(s) if v.get() else self.statuses.discard(s)
                    self.filter_changed()
                check = tk.Checkbutton(choices, text=label, variable=var, command=toggle, bg=BG, anchor="w", font=FONT, relief="flat", pady=5)
                check._variable = var
                check.pack(fill="x")
        query.trace_add("write", render)
        render()
        pop.show(entry)

    def show_actions(self, task_id, anchor):
        self.selected_id = task_id
        task = self.app.tasks.get(task_id)
        if not task:
            self.refresh(force=True)
            return
        self._dismiss_popup()
        pop = self.popup = AnchoredPopover(self, anchor, 192)
        body = tk.Frame(pop, bg=BG, padx=6, pady=6)
        body.pack(fill="both")
        actions = [("查看任务详情", lambda: self.details(task_id))]
        if result_files(task):
            actions += [("打开图片", lambda: self.open_result(task_id)), ("打开文件夹", lambda: self.open_result(task_id, True))]
        if task.get("status") in ("queued", "running"):
            actions.append(("取消任务", lambda: self.cancel(task_id)))
        if retry_policy(task) != "blocked":
            actions.append(("核实后重试" if retry_policy(task) == "confirm" else "恢复未完成图片", lambda: self.retry(task_id)))
        buttons = []
        for label, command in actions:
            def invoke(fn=command):
                self._dismiss_popup()
                fn()
            button = ttk.Button(body, text=label, command=invoke)
            button.pack(fill="x", pady=1)
            buttons.append(button)
        for index, button in enumerate(buttons):
            button.bind("<Down>", lambda e, i=index: buttons[(i + 1) % len(buttons)].focus_set())
            button.bind("<Up>", lambda e, i=index: buttons[(i - 1) % len(buttons)].focus_set())
        pop.show(buttons[0])

    def details(self, task_id):
        self.selected_id = task_id
        task = self.app.tasks.get(task_id)
        if not task:
            return
        content = ["任务：" + str(task.get("name", task_id)), "任务 ID：" + task_id,
                   task_summary(task), elapsed_text(task), "创建：" + readable_time(task.get("created_at")),
                   "中转源：" + "、".join(task.get("sources", [])),
                   "请求尺寸：" + str(task.get("size", "—"))]
        for number, item in enumerate(task.get("items", []), 1):
            content.extend(["", "第 %d 张 · %s" % (number, STATUS.get(item.get("status"), ("", "未知", ""))[1]),
                            "提示词：" + str(item.get("prompt", ""))])
            if item.get("error"):
                content.append("原因：" + str(item["error"]))
            if item.get("output"):
                content.append("生成结果：\n" + json.dumps(item["output"], ensure_ascii=False, indent=2))
            if item.get("attempts"):
                content.append("尝试记录：\n" + json.dumps(item["attempts"], ensure_ascii=False, indent=2))
        return DetailDialog(self, "任务详情", self.app.safe_text("\n".join(content), 100000))

    def cancel(self, task_id):
        self.app.tasks.cancel(task_id)
        self.app.set_status("正在提交取消请求…", "off")
        def report():
            if not self.winfo_exists():
                return
            state = self.app.tasks.cancellation(task_id)
            if state.get("status") == "pending":
                self.after(80, report)
                return
            if state.get("status") == "failed":
                self.app.set_status(self.app.safe_text("取消失败，可再次取消：" + state.get("error", ""), 300), "off")
            else:
                self.app.set_status("取消请求已保存；在途请求的结果需要核实", "off")
            self.refresh(force=True)
        self.after(80, report)

    def retry(self, task_id):
        task = self.app.tasks.get(task_id)
        if not task:
            return
        confirm = retry_policy(task) == "confirm"
        if confirm and not messagebox.askyesno("重试可能重复计费", "上次请求的结果尚未确认，服务端可能已经生成图片并计费。\n\n请先核对中转源账单或结果。仍要重新提交未确认的图片吗？", parent=self):
            return
        try:
            self.app.tasks.retry(task_id, confirm_unknown=confirm)
            self.app.set_status("已开始恢复；已完成的图片会保留")
        except (OSError, ValueError, RuntimeError) as error:
            messagebox.showerror("无法恢复", self.app.safe_text(str(error)), parent=self)
        self.refresh(force=True)

    def open_result(self, task_id, folder=False):
        task = self.app.tasks.get(task_id) or {}
        files = result_files(task)
        try:
            if not files:
                raise OSError("此任务还没有可打开的图片")
            open_path(os.path.dirname(files[0]) if folder else files[0])
        except OSError as error:
            messagebox.showerror("无法打开", self.app.safe_text(str(error)), parent=self)


class TestImageDialog(tk.Toplevel):
    def __init__(self, app, source_name=None):
        super().__init__(app)
        self.app, self.task_id, self._poll_id = app, None, None
        self.title("测试出图")
        self.geometry("740x560")
        self.minsize(640, 500)
        self.configure(bg=BG)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda e: self.close())
        head = tk.Frame(self, bg=BG)
        head.pack(fill="x", padx=20, pady=(16, 10))
        tk.Label(head, text="测试出图", bg=BG, fg=TEXT, font=(FONT[0], 14, "bold")).pack(side="left")
        ttk.Button(head, text="关闭 ×", command=self.close).pack(side="right")
        self.sources = app.read_sources()
        names = [s["name"] for s in self.sources]
        self.source_var = tk.StringVar(value=source_name if source_name in names else (names[0] if names else ""))
        self.model_var, self.size_var = tk.StringVar(), tk.StringVar(value="1024x1024")
        self.deadline_var = tk.StringVar(value="300")
        fields = tk.Frame(self, bg=BG)
        fields.pack(fill="x", padx=20)
        fields.columnconfigure(1, weight=1)
        fields.columnconfigure(3, weight=1)
        self.controls = []
        for row, column, label, variable, values in (
            (0, 0, "中转源", self.source_var, names),
            (0, 2, "模型", self.model_var, []),
            (1, 0, "尺寸", self.size_var, ["1024x1024", "1536x1024", "1024x1536"]),
            (1, 2, "总时限（秒）", self.deadline_var, None),
        ):
            tk.Label(fields, text=label, bg=BG, font=SMALL).grid(row=row, column=column, sticky="w", padx=(0 if column == 0 else 14, 8), pady=6)
            control = ttk.Entry(fields, textvariable=variable, width=13) if values is None else ttk.Combobox(fields, textvariable=variable, values=values, width=18, state="readonly" if variable is self.source_var else "normal")
            control.grid(row=row, column=column + 1, sticky="ew", pady=6)
            self.controls.append(control)
            if variable is self.model_var:
                self.model_box = control
            if variable is self.size_var:
                self.size_box = control
        self.capabilities = tk.Label(self, bg=BG, fg=MUTED, font=SMALL, anchor="w", justify="left", wraplength=680)
        self.capabilities.pack(fill="x", padx=20, pady=(4, 4))
        self.source_var.trace_add("write", self._source_changed)
        self._source_changed()
        self.cost_label = tk.Label(self, text="点击「生成测试图片」会向选中中转源发起真实请求，可能产生费用。仅测试该源；不会自动重复未知结果。", bg="#fff8c5", fg="#7d4e00", font=SMALL, justify="left", anchor="w", padx=10, pady=8, wraplength=680)
        self.cost_label.pack(fill="x", padx=20, pady=(4, 10))
        prompt_row = tk.Frame(self, bg=BG)
        prompt_row.pack(fill="both", expand=True, padx=20)
        tk.Label(prompt_row, text="提示词", bg=BG, font=SMALL).pack(side="left", anchor="n", padx=(0, 10), pady=6)
        self.prompt = tk.Text(prompt_row, height=4, wrap="word", font=FONT, relief="solid", borderwidth=1, padx=8, pady=6)
        self.prompt.pack(side="left", fill="both", expand=True)
        self.prompt.insert("1.0", "一只橙色猫坐在窗边，柔和日光，简洁插画，无文字。")
        actions = tk.Frame(self, bg=BG)
        actions.pack(fill="x", padx=20, pady=12)
        self.generate_button = ttk.Button(actions, text="生成测试图片", style="Primary.TButton", command=self.generate)
        self.generate_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消任务", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        ttk.Button(actions, text="任务历史", command=self.open_history).pack(side="left")
        self.status_var = tk.StringVar(value="等待开始。模型列表可达不代表能成功出图。")
        self.status_label = tk.Label(self, textvariable=self.status_var, bg=BG, fg=MUTED, font=SMALL, justify="left", anchor="w", wraplength=680)
        self.status_label.pack(fill="x", padx=20, pady=(0, 10))
        result_actions = tk.Frame(self, bg=BG)
        result_actions.pack(fill="x", padx=20, pady=(0, 16))
        self.image_button = ttk.Button(result_actions, text="打开图片", command=lambda: self.open_result(), state="disabled")
        self.image_button.pack(side="left")
        self.folder_button = ttk.Button(result_actions, text="打开文件夹", command=lambda: self.open_result(True), state="disabled")
        self.folder_button.pack(side="left", padx=8)
        ttk.Button(result_actions, text="任务详情", command=self.details).pack(side="left")
        self.bind("<Configure>", self._resize)

    def _resize(self, event):
        if event.widget is self:
            for widget in (self.capabilities, self.cost_label, self.status_label):
                widget.config(wraplength=max(400, event.width - 60))

    def _source_changed(self, *_args):
        source = next((s for s in self.sources if s["name"] == self.source_var.get()), {})
        models = source.get("models") or [source.get("model", "")]
        self.model_box.config(values=models)
        self.model_var.set(models[0] if models else "")
        sizes = source.get("supported_sizes") or []
        self.size_box.config(values=sizes or ["1024x1024", "1536x1024", "1024x1536"])
        self.size_var.set("1024x1024" if not sizes or "1024x1024" in sizes else sizes[0])
        known = "已配置支持尺寸：" + "、".join(map(str, sizes)) if sizes else "未声明支持尺寸；默认用 1024×1024 测试，能力以本次结果为准。"
        if source.get("supported_models"):
            known += "\n已配置模型能力：" + "、".join(source["supported_models"])
        self.capabilities.config(text=self.app.safe_text(known, 500))

    def generate(self):
        if self.task_id in self.app.tasks.active_ids():
            return
        try:
            self.task_id = self.app.tasks.generate(self.source_var.get(), self.model_var.get(), self.size_var.get(), self.deadline_var.get(), self.prompt.get("1.0", "end").strip())
        except (OSError, ValueError, RuntimeError) as error:
            self.status_var.set(self.app.safe_text(str(error)))
            return
        self.generate_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.image_button.config(state="disabled")
        self.folder_button.config(state="disabled")
        self.status_var.set("正在启动…关闭此窗口后任务仍可在任务历史查看。")
        self._poll()

    def _poll(self):
        if not self.winfo_exists() or not self.task_id:
            return
        try:
            task = self.app.tasks.get(self.task_id)
        except Exception as error:
            self.status_var.set("读取任务失败：" + self.app.safe_text(str(error)))
            task = None
        active = self.task_id in self.app.tasks.active_ids()
        cancellation = self.app.tasks.cancellation(self.task_id)
        if task:
            text = task_summary(task) + "\n" + elapsed_text(task)
            output = next((item.get("output") for item in task.get("items", []) if item.get("output")), None)
            if output:
                text += " · 实际尺寸 %s×%s" % (output.get("width", "?"), output.get("height", "?"))
                self.image_button.config(state="normal")
                self.folder_button.config(state="normal")
            if cancellation.get("status") == "failed":
                text += "\n取消失败；任务尚未取消，可再次点击取消：" + cancellation.get("error", "")
            elif cancellation.get("status") == "pending":
                text += "\n正在提交取消请求…"
            self.status_var.set(self.app.safe_text(text, 650))
        elif not active:
            self.status_var.set("引擎启动失败：" + self.app.safe_text(self.app.tasks.logs.get(self.task_id, "未返回任务记录"), 500))
        if active:
            cancel_pending = cancellation.get("status") in ("pending", "requested") or bool(task and task.get("cancel_requested"))
            self.cancel_button.config(state="disabled" if cancel_pending else "normal")
            self._poll_id = self.after(300, self._poll)
        else:
            self.generate_button.config(state="normal")
            self.cancel_button.config(state="disabled")
            self.app.pages["history"].refresh(force=True)

    def cancel(self):
        if self.task_id:
            self.app.tasks.cancel(self.task_id)
            self.cancel_button.config(state="disabled")
            self.status_var.set("正在提交取消请求…保存成功前任务仍可能继续。")

    def details(self):
        if self.task_id:
            return self.app.pages["history"].details(self.task_id)
        self.status_var.set("尚未开始测试；生成后可查看请求、尝试记录与结果。")

    def open_result(self, folder=False):
        if self.task_id:
            self.app.pages["history"].open_result(self.task_id, folder)

    def open_history(self):
        self.app.show_page("history")
        self.close()

    def close(self):
        if self._poll_id:
            self.after_cancel(self._poll_id)
        if self.task_id in self.app.tasks.active_ids():
            self.app.set_status("测试仍在运行；请在任务历史查看或取消")
        self.destroy()
