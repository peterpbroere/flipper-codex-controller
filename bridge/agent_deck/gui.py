"""Small Tk companion; USB and SQLite work stay outside the UI thread."""
from concurrent.futures import ThreadPoolExecutor
import queue
import tkinter as tk
from tkinter import ttk, messagebox

from .companion import Companion


BG = "#f4f3ef"
INK = "#222b29"
MUTED = "#61706b"
ACCENT = "#bd4c1b"


class CompanionWindow:
    def __init__(self, root, controller=None):
        self.root = root
        self.controller = controller or Companion()
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-deck-window")
        self.results = queue.Queue()
        self.busy = False
        self.closed = False
        self.pending = 0
        self.action_busy = False
        self.action_error = ""
        self.rows = {}
        self.selected = None
        self.health = {}
        self.last_logs = ""
        self.error = tk.StringVar()
        self.notice = tk.StringVar(value="Closing this window keeps the bridge running.")
        self.name = tk.StringVar()
        self.project = tk.StringVar(value="Select a session to view its details.")
        self.session = tk.StringVar(value="")
        self.summary = tk.StringVar(value="Loading sessions…")
        self.connection = tk.StringVar(value="Checking connection…")
        self.connection_hint = tk.StringVar(value="")
        self.logs_visible = False
        root.title("Agent Deck — Companion")
        root.geometry("960x850")
        root.minsize(840, 750)
        root.configure(background=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._style()
        self._layout()
        self._submit(self.controller.refresh)
        self.poll_timer = root.after(100, self._poll)
        self.refresh_timer = root.after(900, self._refresh)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
        style.configure("Section.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(13, 8))
        style.configure("Accent.TButton", background=ACCENT, foreground="white", borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#a13d13"), ("disabled", "#d6c9c1")])
        style.configure("TEntry", padding=7, font=("Segoe UI", 11))
        style.configure("Treeview", background="white", fieldbackground="white", foreground=INK,
                        rowheight=36, font=("Segoe UI", 10), borderwidth=0)
        style.configure("Treeview.Heading", background="#e5e7e0", foreground=MUTED,
                        font=("Segoe UI", 10, "bold"), padding=(10, 9), relief="flat")
        style.map("Treeview", background=[("selected", "#dce7de")], foreground=[("selected", INK)])

    def _layout(self):
        body = ttk.Frame(self.root, padding=28)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        title = ttk.Frame(body)
        title.grid(row=0, column=0, sticky="ew")
        ttk.Label(title, text="Agent Deck", style="Title.TLabel").pack(side="left")
        ttk.Label(body, text="Your coding sessions, within reach.", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 20))
        status = ttk.Frame(body)
        status.grid(row=2, column=0, sticky="ew", pady=(0, 18))
        left = ttk.Frame(status)
        left.pack(side="left", fill="x", expand=True)
        self.status_label = ttk.Label(left, textvariable=self.connection, style="Status.TLabel")
        self.status_label.pack(anchor="w")
        ttk.Label(left, textvariable=self.connection_hint, style="Muted.TLabel", wraplength=440).pack(anchor="w", pady=(4, 0))
        self.restart = ttk.Button(status, text="Restart", command=lambda: self._control("restart"))
        self.restart.pack(side="right", padx=(8, 0))
        self.toggle = ttk.Button(status, text="Start bridge", style="Accent.TButton", command=self._toggle)
        self.toggle.pack(side="right")
        heading = ttk.Frame(body)
        heading.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(heading, text="Sessions", style="Section.TLabel").pack(side="left")
        ttk.Label(heading, textvariable=self.summary, style="Muted.TLabel").pack(side="right")
        table = ttk.Frame(body)
        table.grid(row=4, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.table = ttk.Treeview(table, columns=("name", "state", "project"), show="headings", selectmode="browse", height=5)
        for key, label, width in (("name", "NAME", 225), ("state", "STATUS", 135), ("project", "PROJECT", 420)):
            self.table.heading(key, text=label, anchor="w")
            self.table.column(key, width=width, minwidth=90, stretch=key != "state")
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.tag_configure("waiting", foreground="#a33d17")
        self.table.tag_configure("error", foreground="#b02c31")
        self.table.bind("<<TreeviewSelect>>", self._select)
        self.table.bind("<Double-1>", lambda event: self.name_entry.focus_set())
        self.empty = ttk.Label(body, text="New sessions appear after their first prompt.", style="Muted.TLabel")
        self.empty.grid(row=5, column=0, sticky="w", pady=(7, 15))
        details = ttk.Frame(body)
        details.grid(row=6, column=0, sticky="ew", pady=(0, 12))
        details.columnconfigure(0, weight=1)
        ttk.Label(details, text="Session name", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.name_entry = ttk.Entry(details, textvariable=self.name, font=("Segoe UI", 11))
        self.name_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.name_entry.bind("<Return>", lambda event: self._rename())
        self.save = ttk.Button(details, text="Save name", style="Accent.TButton", command=self._rename)
        self.save.grid(row=1, column=1, padx=(0, 8))
        self.reset = ttk.Button(details, text="Use project name", command=lambda: self._rename(reset=True))
        self.reset.grid(row=1, column=2)
        project_label = ttk.Label(details, textvariable=self.project, style="Muted.TLabel", wraplength=850)
        project_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 4))
        session_label = ttk.Label(details, textvariable=self.session, style="Muted.TLabel", wraplength=850)
        session_label.grid(row=3, column=0, columnspan=3, sticky="w")
        actions = ttk.Frame(details)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.agent_buttons = {}
        for verb, label in (("focus", "Open on PC"), ("stop", "Stop turn"),
                            ("continue", "Continue"), ("approve", "Review & approve"), ("reject", "Reject")):
            button = ttk.Button(actions, text=label, command=lambda v=verb: self._agent_action(v))
            button.pack(side="left", padx=(0, 6))
            self.agent_buttons[verb] = button
        self.control_hint = tk.StringVar(value="Select a session to see its controls.")
        ttk.Label(details, textvariable=self.control_hint, style="Muted.TLabel", wraplength=800).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        error_label = ttk.Label(body, textvariable=self.error, foreground="#a92d25", wraplength=850)
        error_label.grid(row=7, column=0, sticky="w")
        body.bind("<Configure>", lambda event: [widget.configure(wraplength=max(300, event.width - 56))
                                               for widget in (project_label, session_label, error_label)])
        footer = ttk.Frame(body)
        footer.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(footer, textvariable=self.notice, style="Muted.TLabel", wraplength=640).pack(side="left")
        self.activity_button = ttk.Button(footer, text="Show activity", command=self._toggle_logs)
        self.activity_button.pack(side="right")
        self.activity = ttk.Frame(body)
        self.activity.grid(row=9, column=0, sticky="nsew", pady=(12, 0))
        self.log = tk.Text(self.activity, height=6, font=("Consolas", 9), background="#202a27", foreground="#e1e8e2",
                           relief="flat", wrap="word", padx=12, pady=10, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(self.activity, command=self.log.yview)
        log_scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=log_scroll.set)
        self.activity.grid_remove()
        self._buttons()

    def _submit(self, operation, notice=None, action=False, editor_key=None):
        if action and self.action_busy:
            return
        self.pending += 1
        self.busy = True
        if action:
            self.action_busy = True
            self.action_error = ""
        self._buttons()
        results = self.results
        def perform():
            try:
                results.put((operation(), None, notice, action, editor_key))
            except Exception as error:
                results.put((None, str(error), None, action, editor_key))
        self.worker.submit(perform)

    def _refresh(self):
        if self.closed:
            return
        if not self.busy:
            self._submit(self.controller.refresh)
        self.refresh_timer = self.root.after(900, self._refresh)

    def _poll(self):
        if self.closed:
            return
        try:
            while True:
                data, error, notice, action, editor_key = self.results.get_nowait()
                self.pending -= 1
                self.busy = self.pending > 0
                if action:
                    self.action_busy = False
                if data is not None:
                    self._render(data)
                if error:
                    if action:
                        self.action_error = error
                    self.error.set(error)
                elif notice:
                    self.notice.set(notice)
                    if editor_key == self.selected and editor_key in self.rows:
                        row = self.rows[editor_key]
                        self.name.set(row.get("name") or row.get("automatic_name") or row["label"])
                self._buttons()
        except queue.Empty:
            pass
        self.poll_timer = self.root.after(100, self._poll)

    def _render(self, data):
        self.health = data["health"]
        self.connection.set(self.health["message"])
        connected = self.health.get("connected")
        self.status_label.configure(foreground="#287146" if connected else MUTED)
        if connected:
            hint = f"USB · {self.health.get('port', '')} · " + (
                "Demo mode: the Flipper shows sample sessions" if self.health.get("mode") == "demo" else "Notifications are active")
        elif self.health.get("running") is None:
            hint = "Bridge controls are unavailable until its process can be verified."
        elif self.health.get("running"):
            hint = "Open Apps → Tools → Agent Deck on your Flipper and connect a USB data cable."
        else:
            hint = "Start the bridge to send session updates to your Flipper."
        self.connection_hint.set(hint)
        self.error.set(self.action_error or data.get("error", ""))
        self.rows = {row["id"]: row for row in data["rows"]}
        for key in self.table.get_children():
            if key not in self.rows:
                self.table.delete(key)
        for index, row in enumerate(data["rows"]):
            values = (row["label"], row["reason"] if row["state"] == "waiting" else row["state"].upper(), row["project"] or "Not reported yet")
            if self.table.exists(row["id"]):
                self.table.item(row["id"], values=values, tags=(row["state"],))
                self.table.move(row["id"], "", index)
            else:
                self.table.insert("", "end", iid=row["id"], values=values, tags=(row["state"],))
        attention = sum(row["state"] == "waiting" for row in data["rows"])
        self.summary.set(f"{len(self.rows)} {'session' if len(self.rows) == 1 else 'sessions'} · {attention} need attention")
        self.empty.configure(text="New sessions appear after their first prompt." if not self.rows else
                             "Select a session to name it. Changes appear on the Flipper automatically.")
        if self.selected not in self.rows:
            self.selected = None
            self.name.set("")
            self.project.set("Select a session to view its details.")
            self.session.set("")
        if self.selected is None and self.rows:
            self.table.selection_set(next(iter(self.rows)))
        if self.selected in self.rows:
            row = self.rows[self.selected]
            self.project.set("Project: " + (row["project"] or "Not reported yet"))
            self.session.set("Session: " + row["session_id"])
        if data["logs"] != self.last_logs:
            self.last_logs = data["logs"]
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("1.0", self.last_logs or "No bridge activity yet.")
            self.log.see("end")
            self.log.configure(state="disabled")

    def _select(self, event=None):
        selection = self.table.selection()
        key = selection[0] if selection else None
        if key == self.selected:
            return  # Polling must not overwrite an edit in progress.
        self.selected = key
        if key in self.rows:
            row = self.rows[key]
            self.name.set(row.get("name") or row.get("automatic_name") or row["label"])
            self.project.set("Project: " + (row["project"] or "Not reported yet"))
            self.session.set("Session: " + row["session_id"])
        self._buttons()

    def _buttons(self):
        enabled = not self.action_busy and self.selected in self.rows
        for widget in (self.name_entry, self.save, self.reset):
            widget.configure(state="normal" if enabled else "disabled")
        running = self.health.get("running")
        self.toggle.configure(text="Stop bridge" if running else "Start bridge",
                              state="disabled" if self.action_busy or running is None else "normal")
        self.restart.configure(state="normal" if running and not self.action_busy else "disabled")
        row = self.rows.get(self.selected, {})
        for verb, button in self.agent_buttons.items():
            button.configure(state="normal" if enabled and verb in row.get("actions", []) else "disabled")
        self.control_hint.set(row.get("control_hint", "Select a session to see its controls."))

    def _agent_action(self, verb):
        row = self.rows.get(self.selected)
        if self.action_busy or not row or verb not in row.get("actions", []):
            return
        key, token, name = self.selected, row.get("control_token", "-"), row["label"]
        controller = self.controller
        def send():
            self._submit(lambda: controller.act(key, verb, token),
                         {"focus": "Session opened on PC.", "stop": "Interrupt requested.",
                          "continue": "Follow-up sent.", "approve": "Approval confirmed.",
                          "reject": "Rejection confirmed."}[verb], action=True)
        if verb == "focus":
            send()
            return
        if verb in {"approve", "reject"}:
            dialog = tk.Toplevel(self.root)
            dialog.title(("Approve once" if verb == "approve" else "Reject request") + " — " + name)
            dialog.geometry("760x500")
            dialog.transient(self.root)
            body = ttk.Frame(dialog, padding=18)
            body.pack(fill="both", expand=True)
            ttk.Label(body, text=name, style="Section.TLabel").pack(anchor="w", pady=(0, 10))
            text = tk.Text(body, wrap="word", font=("Consolas", 10), padx=10, pady=10)
            scroll = ttk.Scrollbar(text, command=text.yview)
            scroll.pack(side="right", fill="y")
            text.configure(yscrollcommand=scroll.set)
            text.insert("1.0", row.get("review") or "Review the pending request in Codex on PC.")
            text.configure(state="disabled")
            text.pack(fill="both", expand=True)
            footer = ttk.Frame(body)
            footer.pack(fill="x", pady=(12, 0))
            cancel = ttk.Button(footer, text="Cancel", command=dialog.destroy)
            cancel.pack(side="right")
            def confirm():
                dialog.destroy()
                send()  # Captured request token, even if the selection has changed.
            ttk.Button(footer, text="Approve once" if verb == "approve" else "Reject this request",
                       command=confirm).pack(side="right", padx=8)
            dialog.bind("<Escape>", lambda event: dialog.destroy())
            cancel.focus_set()
            dialog.grab_set()
            return
        prompt = ("Interrupt the current turn?" if verb == "stop" else
                  'Send this follow-up?\n\n“Continue with the next logical step.”')
        if messagebox.askyesno(name, prompt, parent=self.root, default="no"):
            send()

    def _rename(self, reset=False):
        if self.selected not in self.rows or self.action_busy:
            return
        key, name = self.selected, None if reset else self.name.get()
        controller = self.controller
        def save():
            return controller.rename(key, name)
        self._submit(save, "Project name restored." if reset else "Session name saved.", action=True, editor_key=key)

    def _toggle(self):
        self._control("stop" if self.health.get("running") else "start")

    def _control(self, action):
        controller = self.controller
        self._submit(lambda: controller.control(action),
                     {"start": "Bridge started.", "stop": "Bridge stopped.", "restart": "Bridge restarted."}[action], action=True)

    def _toggle_logs(self):
        self.logs_visible = not self.logs_visible
        if self.logs_visible:
            self.activity.grid()
        else:
            self.activity.grid_remove()
        self.activity_button.configure(text="Hide activity" if self.logs_visible else "Show activity")

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.root.after_cancel(self.poll_timer)
        self.root.after_cancel(self.refresh_timer)
        self.worker.submit(self.controller.close)
        self.worker.shutdown(wait=False)
        self.root.destroy()


def launch(store_path=None):
    root = tk.Tk()
    CompanionWindow(root, Companion(store_path))
    root.mainloop()
