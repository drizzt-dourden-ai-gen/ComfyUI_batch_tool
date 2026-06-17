"""
comfy_batch_ui.py
─────────────────
Desktop UI for ComfyUI multi-workflow batch generation.
Detects workflow JSON files in the same folder, lets you configure
seed ranges, filename prefixes, enable/disable toggles, saves settings
to config.json, and runs generation with a live log display.

Requirements:
    pip install websocket-client requests
    tkinter is included with standard Python on Windows

Place this script in the same folder as your workflow JSON files.
Create a desktop shortcut pointing to:
    pythonw.exe comfy_batch_ui.py
(pythonw suppresses the terminal window)
"""

import json
import os
import time
import threading
import uuid
import tkinter as tk
from tkinter import ttk, scrolledtext
import requests
import websocket

# ── Paths ──────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_SERVER = "127.0.0.1:8188"
DEFAULT_DELAY  = 45
DEFAULT_RANGE  = {"start": 1, "stop": 50, "enabled": False, "prefix": ""}

# ── Config load/save ───────────────────────────────────────────────────────────

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(data: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ── Workflow file detection ────────────────────────────────────────────────────

def find_workflows() -> list[str]:
    return sorted([
        f for f in os.listdir(SCRIPT_DIR)
        if f.endswith(".json")
        and not f.endswith("_config.json")
        and f != "config.json"
    ])


# ── Generation engine ──────────────────────────────────────────────────────────

class GenerationEngine:
    def __init__(self, log_fn, done_fn):
        self.log               = log_fn
        self.done              = done_fn
        self.timer             = None
        self.timer_lk          = threading.Lock()
        self.busy              = False
        self.running           = False
        self.ws_app            = None
        self.flat_q            = []
        self.flat_idx          = 0
        self.flat_seed         = 0
        self.server            = DEFAULT_SERVER
        self.delay             = DEFAULT_DELAY
        self.pending_prompt_id = None
        self.client_id         = str(uuid.uuid4())

    def start(self, server: str, delay: int, workflows: list[dict]):
        self.server            = server
        self.delay             = delay
        self.running           = True
        self.busy              = False
        self.flat_idx          = 0
        self.pending_prompt_id = None

        self.flat_q = []
        for wf in workflows:
            if not wf.get("enabled", True):
                continue
            for r in wf.get("ranges", []):
                if not r.get("enabled", True):
                    continue
                self.flat_q.append({
                    "file":   wf["file"],
                    "start":  r["start"],
                    "stop":   r["stop"],
                    "prefix": r.get("prefix", "").strip(),
                })

        if not self.flat_q:
            self.log("ERROR: No enabled workflows or ranges. Nothing to run.")
            self.done()
            return

        self.flat_seed = self.flat_q[0]["start"]
        self.log("=" * 52)
        self.log("  ComfyUI Batch Runner — queue summary")
        for i, e in enumerate(self.flat_q, 1):
            count  = e["stop"] - e["start"] + 1
            prefix = f"  prefix={e['prefix']}" if e["prefix"] else ""
            self.log(f"  [{i}] {e['file']}  seeds {e['start']}–{e['stop']}  ({count} images){prefix}")
        self.log("=" * 52)

        t = threading.Thread(target=self._connect, daemon=True)
        t.start()

    def stop(self):
        self.running = False
        self._cancel_timer()
        if self.ws_app:
            try:
                self.ws_app.close()
            except Exception:
                pass
        self.log("⏹  Stopped by user.")
        self.done()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _connect(self):
        url = f"ws://{self.server}/ws?clientId={self.client_id}"
        self.ws_app = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self.ws_app.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws):
        self.log(f"🟢 Connected to ComfyUI at {self.server}")
        self._trigger()

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        data     = msg.get("data", {})

        if msg_type == "execution_start":
            pid = data.get("prompt_id")
            if pid == self.pending_prompt_id:
                self.log("🚀 Generation started")
                self._cancel_timer()

        elif msg_type == "executing":
            pid  = data.get("prompt_id")
            node = data.get("node")
            if pid == self.pending_prompt_id and node is None:
                self.log("✔  Generation finished")
                self.busy              = False
                self.pending_prompt_id = None
                if self.running:
                    self._start_timer()

        elif msg_type == "execution_error":
            pid = data.get("prompt_id")
            if pid == self.pending_prompt_id:
                self.log(f"⚠  Execution error — retrying seed {self.flat_seed - 1} in {self.delay}s")
                self.busy              = False
                self.pending_prompt_id = None
                self.flat_seed -= 1
                if self.running:
                    self._start_timer()

    def _on_error(self, ws, error):
        self.log(f"WebSocket error: {error}")

    def _on_close(self, ws, code, msg):
        if not self.running:
            return
        self.log("WebSocket closed — reconnecting in 5s …")
        time.sleep(5)
        if self.running:
            self._connect()

    def _cancel_timer(self):
        with self.timer_lk:
            if self.timer:
                self.timer.cancel()
                self.timer = None

    def _start_timer(self):
        self._cancel_timer()
        with self.timer_lk:
            self.log(f"🕒 Next generation in {self.delay}s …")
            self.timer = threading.Timer(self.delay, self._trigger)
            self.timer.daemon = True
            self.timer.start()

    def _trigger(self):
        if not self.running:
            return

        if self.flat_idx >= len(self.flat_q):
            self.log("🏁 All ranges complete.")
            self.running = False
            self.done()
            return

        entry = self.flat_q[self.flat_idx]

        if self.flat_seed > entry["stop"]:
            self.flat_idx += 1
            if self.flat_idx >= len(self.flat_q):
                self.log("🏁 All ranges complete.")
                self.running = False
                self.done()
                return
            entry = self.flat_q[self.flat_idx]
            self.flat_seed = entry["start"]
            self.log(f"▶  Switching to {entry['file']}  seeds {entry['start']}–{entry['stop']}")

        self.log(f"⏱  {entry['file']}  seed {self.flat_seed}/{entry['stop']}")

        try:
            path = os.path.join(SCRIPT_DIR, entry["file"])
            with open(path, "r", encoding="utf-8") as f:
                workflow = json.load(f)

            for node in workflow.values():
                inputs = node.get("inputs", {})

                # Inject seed
                for key in ("seed", "noise_seed"):
                    if key in inputs and isinstance(inputs[key], int):
                        inputs[key] = self.flat_seed

                # Override filename prefix if set
                if entry["prefix"] and node.get("class_type") in ("SaveImage", "Image Save"):
                    inputs["filename_prefix"] = entry["prefix"]

            payload = {"prompt": workflow, "client_id": self.client_id}
            r = requests.post(f"http://{self.server}/prompt", json=payload, timeout=10)
            r.raise_for_status()
            pid = r.json()["prompt_id"]

            self.pending_prompt_id = pid
            self.busy              = True
            prefix_note = f"  prefix={entry['prefix']}" if entry["prefix"] else ""
            self.log(f"✅ Queued seed {self.flat_seed}  (id={pid[:8]}…){prefix_note}")
            self.flat_seed += 1

        except Exception as e:
            self.log(f"❌ {e}")


# ── UI ─────────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ComfyUI Batch Runner")
        self.resizable(True, True)
        self.minsize(700, 500)
        self.configure(bg="#1e1e1e")

        self.cfg      = load_config()
        self.wf_rows  = []
        self.engine   = GenerationEngine(self._log, self._on_done)

        self._build_ui()
        self._load_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        PAD  = 8
        BG   = "#1e1e1e"
        BG2  = "#2d2d2d"
        BG3  = "#252525"
        FG   = "#d4d4d4"
        ACC  = "#4ec9b0"
        FONT = ("Consolas", 9)

        self.configure(bg=BG)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame",       background=BG)
        style.configure("TLabel",       background=BG,  foreground=FG,  font=FONT)
        style.configure("TCheckbutton", background=BG2, foreground=FG,  font=FONT,
                        indicatormargin=4)
        style.map("TCheckbutton",       background=[("active", BG2)])
        style.configure("TEntry",       fieldbackground=BG2, foreground=FG,
                        insertcolor=FG, font=FONT, relief="flat")
        style.configure("TScrollbar",   background=BG2, troughcolor=BG, arrowcolor=FG)

        # ── Top bar ────────────────────────────────────────────────────────────
        top = ttk.Frame(self, padding=(PAD, PAD, PAD, 4))
        top.pack(fill="x")

        ttk.Label(top, text="Server:").pack(side="left")
        self.sv_server = tk.StringVar(value=self.cfg.get("server", DEFAULT_SERVER))
        ttk.Entry(top, textvariable=self.sv_server, width=22).pack(side="left", padx=(4, 12))

        ttk.Label(top, text="Delay (s):").pack(side="left")
        self.sv_delay = tk.StringVar(value=str(self.cfg.get("delay", DEFAULT_DELAY)))
        ttk.Entry(top, textvariable=self.sv_delay, width=6).pack(side="left", padx=(4, 12))

        tk.Button(top, text="⟳ Refresh", font=FONT, bg=BG2, fg=FG,
                  relief="flat", activebackground="#3a3a3a", activeforeground=FG,
                  cursor="hand2", command=self._refresh_workflows
                  ).pack(side="left", padx=(0, 6))

        self.btn_start = tk.Button(
            top, text="▶  Start", font=("Consolas", 9, "bold"),
            bg="#1a6b4a", fg="white", relief="flat",
            activebackground="#1e7d56", activeforeground="white",
            cursor="hand2", command=self._start, width=10)
        self.btn_start.pack(side="right", padx=(4, 0))

        self.btn_stop = tk.Button(
            top, text="■  Stop", font=("Consolas", 9, "bold"),
            bg="#6b1a1a", fg="white", relief="flat",
            activebackground="#7d1e1e", activeforeground="white",
            cursor="hand2", command=self._stop, width=10, state="disabled")
        self.btn_stop.pack(side="right", padx=(4, 0))

        # ── Workflow area (scrollable) ──────────────────────────────────────────
        mid = ttk.Frame(self, padding=(PAD, 0, PAD, 0))
        mid.pack(fill="both", expand=True)

        ttk.Label(mid, text="Workflows", font=("Consolas", 9, "bold"),
                  foreground=ACC).pack(anchor="w", pady=(4, 2))

        canvas_frame = tk.Frame(mid, bg=BG)
        canvas_frame.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0)
        sb = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.wf_frame = tk.Frame(self.canvas, bg=BG)
        self.canvas_win = self.canvas.create_window((0, 0), window=self.wf_frame, anchor="nw")
        self.wf_frame.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(
            self.canvas_win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        # ── Log area ───────────────────────────────────────────────────────────
        log_frame = ttk.Frame(self, padding=(PAD, 4, PAD, PAD))
        log_frame.pack(fill="x")

        ttk.Label(log_frame, text="Log", font=("Consolas", 9, "bold"),
                  foreground=ACC).pack(anchor="w")

        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=10, font=("Consolas", 8),
            bg=BG3, fg=FG, insertbackground=FG,
            relief="flat", state="disabled", wrap="word")
        self.log_box.pack(fill="x")

        self.log_box.tag_config("green",  foreground="#4ec9b0")
        self.log_box.tag_config("yellow", foreground="#dcdcaa")
        self.log_box.tag_config("red",    foreground="#f44747")
        self.log_box.tag_config("dim",    foreground="#808080")

        self._populate_workflows()

    def _make_wf_row(self, parent, filename: str, saved: dict) -> dict:
        BG   = "#1e1e1e"
        BG2  = "#2d2d2d"
        FG   = "#d4d4d4"
        FONT = ("Consolas", 9)

        row_bg = BG2
        frame = tk.Frame(parent, bg=row_bg, pady=3, padx=6,
                         highlightthickness=1, highlightbackground="#3a3a3a")
        frame.pack(fill="x", pady=3)

        hdr = tk.Frame(frame, bg=row_bg)
        hdr.pack(fill="x")

        wf_enabled = tk.BooleanVar(value=saved.get("enabled", True))
        tk.Checkbutton(hdr, variable=wf_enabled, text=filename,
                       bg=row_bg, fg="#4ec9b0", activebackground=row_bg,
                       activeforeground="#4ec9b0", selectcolor="#1e1e1e",
                       font=("Consolas", 9, "bold"), relief="flat").pack(side="left")

        ranges_data = saved.get("ranges", [
            {"start": 1,     "stop": 50,    "enabled": True,  "prefix": ""},
            {"start": 10001, "stop": 10050, "enabled": False, "prefix": ""},
            {"start": 20001, "stop": 20050, "enabled": False, "prefix": ""},
        ])
        while len(ranges_data) < 3:
            ranges_data.append({"start": 1, "stop": 50, "enabled": False, "prefix": ""})

        range_vars = []
        for i, rd in enumerate(ranges_data[:3]):
            rf = tk.Frame(frame, bg=row_bg)
            rf.pack(fill="x", pady=1)

            r_enabled = tk.BooleanVar(value=rd.get("enabled", False))
            tk.Checkbutton(rf, variable=r_enabled, text=f"Range {i+1}:",
                           bg=row_bg, fg=FG, activebackground=row_bg,
                           activeforeground=FG, selectcolor="#1e1e1e",
                           font=FONT, relief="flat", width=9,
                           anchor="w").pack(side="left")

            tk.Label(rf, text="Start", bg=row_bg, fg="#808080",
                     font=FONT).pack(side="left", padx=(4, 2))
            sv_start = tk.StringVar(value=str(rd.get("start", 1)))
            tk.Entry(rf, textvariable=sv_start, width=8,
                     bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", font=FONT).pack(side="left")

            tk.Label(rf, text="Stop", bg=row_bg, fg="#808080",
                     font=FONT).pack(side="left", padx=(8, 2))
            sv_stop = tk.StringVar(value=str(rd.get("stop", 50)))
            tk.Entry(rf, textvariable=sv_stop, width=8,
                     bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", font=FONT).pack(side="left")

            tk.Label(rf, text="Prefix", bg=row_bg, fg="#808080",
                     font=FONT).pack(side="left", padx=(8, 2))
            sv_prefix = tk.StringVar(value=rd.get("prefix", ""))
            tk.Entry(rf, textvariable=sv_prefix, width=18,
                     bg=BG, fg=FG, insertbackground=FG,
                     relief="flat", font=FONT).pack(side="left")

            range_vars.append({
                "enabled": r_enabled,
                "start":   sv_start,
                "stop":    sv_stop,
                "prefix":  sv_prefix,
            })

        return {
            "file":    filename,
            "enabled": wf_enabled,
            "ranges":  range_vars,
            "frame":   frame,
        }

    def _populate_workflows(self):
        for w in self.wf_frame.winfo_children():
            w.destroy()
        self.wf_rows = []

        files = find_workflows()
        if not files:
            tk.Label(self.wf_frame, text="No workflow JSON files found in this folder.",
                     bg="#1e1e1e", fg="#808080", font=("Consolas", 9)).pack(pady=8)
            return

        saved_wfs = {w["file"]: w for w in self.cfg.get("workflows", [])}
        for fname in files:
            saved = saved_wfs.get(fname, {})
            row = self._make_wf_row(self.wf_frame, fname, saved)
            self.wf_rows.append(row)

    # ── State persistence ──────────────────────────────────────────────────────

    def _collect_state(self) -> dict:
        workflows = []
        for row in self.wf_rows:
            ranges = []
            for rv in row["ranges"]:
                try:
                    start = int(rv["start"].get())
                    stop  = int(rv["stop"].get())
                except ValueError:
                    start, stop = 1, 50
                ranges.append({
                    "enabled": rv["enabled"].get(),
                    "start":   start,
                    "stop":    stop,
                    "prefix":  rv["prefix"].get().strip(),
                })
            workflows.append({
                "file":    row["file"],
                "enabled": row["enabled"].get(),
                "ranges":  ranges,
            })
        try:
            delay = int(self.sv_delay.get())
        except ValueError:
            delay = DEFAULT_DELAY
        return {
            "server":    self.sv_server.get(),
            "delay":     delay,
            "workflows": workflows,
        }

    def _load_state(self):
        pass

    def _save_state(self):
        state = self._collect_state()
        save_config(state)
        self.cfg = state

    # ── Actions ────────────────────────────────────────────────────────────────

    def _refresh_workflows(self):
        self._save_state()
        self._populate_workflows()
        self._log("⟳ Workflow list refreshed.")

    def _start(self):
        self._save_state()
        state = self._collect_state()

        try:
            delay = int(self.sv_delay.get())
        except ValueError:
            self._log("❌ Invalid delay value.")
            return

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")

        self.engine.start(
            server    = state["server"],
            delay     = delay,
            workflows = state["workflows"],
        )

    def _stop(self):
        self.engine.stop()

    def _on_done(self):
        self.after(0, lambda: self.btn_start.config(state="normal"))
        self.after(0, lambda: self.btn_stop.config(state="disabled"))

    def _on_close(self):
        self._save_state()
        self.engine.stop()
        self.destroy()

    # ── Logging ────────────────────────────────────────────────────────────────

    def _log(self, text: str):
        def _write():
            self.log_box.config(state="normal")
            tag = "dim"
            if any(c in text for c in ("🟢", "✅", "✔", "🏁")):
                tag = "green"
            elif any(c in text for c in ("⚠", "❌", "ERROR")):
                tag = "red"
            elif any(c in text for c in ("🚀", "⏱", "▶", "🌱", "🕒")):
                tag = "yellow"
            self.log_box.insert("end", text + "\n", tag)
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        self.after(0, _write)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
