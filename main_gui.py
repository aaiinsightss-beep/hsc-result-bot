# -*- coding: utf-8 -*-
"""wxPython GUI — dark panel, countdown, QUEUE, customizations, advanced.

Architecture (Win + wxPython): network lives in subprocesses/threads that
only touch pipes/requests — wx main thread never does TLS. Pool of tools:

  • Test / Run now / Go / Stop — engine drives main.py subprocess
  • Queue card — add/remove/reorder students; stored in the same secret
    gist serve + cloud read. Save = instant live edit everywhere.
  • Pacing card — day/night check interval, stagger, timeout; serve
    re-tunes live (≤30s), no restart.
  • Advanced card — mirror toggles, start-with-windows, gist id.

Fires:  pythonw main_gui.py   (start_gui.bat)
"""
import datetime
import os
import subprocess
import sys
import threading

import wx
import wx.adv

from engine import load_config, save_config, countdown_target, validate
from queue_store import QueueStore

BASE = os.path.dirname(os.path.abspath(__file__))

DARK = "#0e0f13"
PANE = "#17181f"
LINE = "#2a2b36"
TXT = "#e8e8f0"
DIM = "#8b8d99"
OKC = "#35d98a"
BADC = "#ff5f70"
WARN = "#ffb64d"
BLUE = "#4a78ff"

BOARDS = ["dhaka", "comilla", "chittagong", "rajshahi", "jessore", "khulna",
          "barishal", "sylhet", "dinajpur", "mymensingh", "madrasah", "technical"]
EXAMS = ["hsc", "ssc", "jsc"]

GIST_ID_DEFAULT = "28fa5e7be145307e6592a535d696875b"
STARTUP_BAT = os.path.join(
    os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu",
    "Programs", "Startup", "start_serve.bat")


def hex_c(s: str) -> wx.Colour:
    return wx.Colour(s)


class LogGrid(wx.ListCtrl):
    """Plain (non-virtual) list — per-row colours, MSW-safe append wisdom."""

    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT)
        self.tag_col = {
            "ok": OKC, "err": BADC, "wait": WARN, "pause": WARN,
            "info": DIM, "cf": BLUE,
        }
        self.InsertColumn(0, "time", width=70)
        self.InsertColumn(1, "event", width=300)
        self.SetBackgroundColour(hex_c("#0a0b0f"))
        self.SetForegroundColour(hex_c(DIM))

    def append(self, tag: str, msg: str):
        idx = self.InsertItem(self.GetItemCount(),
                              datetime.datetime.now().strftime("%H:%M:%S"))
        self.SetItem(idx, 1, msg)
        self.SetItemTextColour(idx, hex_c(self.tag_col.get(tag, DIM)))
        self.EnsureVisible(idx)
        if self.GetItemCount() > 400:
            self.DeleteItem(0)


class QueueGrid(wx.ListCtrl):
    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT, size=(-1, 120))
        for i, (name, w) in enumerate([
                ("#", 28), ("name", 90), ("exam", 46), ("board", 76),
                ("year", 46), ("roll", 66), ("reg", 90), ("found", 56)]):
            self.InsertColumn(i, name, width=w)
        self.SetBackgroundColour(hex_c("#0a0b0f"))
        self.SetForegroundColour(hex_c(TXT))

    def fill(self, rows):
        self.DeleteAllItems()
        for i, r in enumerate(rows):
            idx = self.InsertItem(i, str(i + 1))
            vals = [str(r.get("name", "?")), str(r.get("exam", "ssc")),
                    str(r.get("board", "?")), str(r.get("year", "?")),
                    str(r.get("roll", "?")), str(r.get("reg", "?")),
                    "✅" if r.get("found_at") else "⏳"]
            for c, v in enumerate(vals, 1):
                self.SetItem(idx, c, v)
            self.SetItemTextColour(
                idx, hex_c(OKC if r.get("found_at") else TXT))


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None, title="HSC Bot", size=(480, 1030),
            style=wx.DEFAULT_FRAME_STYLE,
        )
        self.SetBackgroundColour(hex_c(DARK))
        self.proc = None
        self.serve_proc = None
        self.timer = None
        self.countdown_target_iso = None
        self.q_rows = []
        self.q_store = None
        self._gist_lock = threading.Lock()
        self._build_ui()
        self._load_cfg_into_form()
        self.queue_reload()
        self._refresh_serve_btn()

    # ----- UI construction -----------------------------------------------
    def _font(self, size, bold=False, mono=False):
        fi = wx.FontInfo(size)
        if mono:
            fi = fi.Family(wx.FONTFAMILY_TELETYPE)
        if bold:
            fi = fi.Bold()
        return wx.Font(fi)

    def _build_ui(self):
        panel = self.panel = wx.Panel(self)
        panel.SetBackgroundColour(hex_c(DARK))

        def flabel(parent, text):
            l = wx.StaticText(parent, label=text)
            l.SetForegroundColour(hex_c(DIM))
            return l

        def finput(parent):
            t = wx.TextCtrl(parent, size=(-1, 26))
            t.SetBackgroundColour(hex_c("#1e1f28"))
            t.SetForegroundColour(hex_c(TXT))
            t.SetFont(self._font(9))
            return t

        def fnum(parent):
            return finput(parent)

        def card(label):
            c = wx.Panel(panel)
            c.SetBackgroundColour(hex_c(PANE))
            cv = wx.BoxSizer(wx.VERTICAL)
            txt = wx.StaticText(c, label=label)
            txt.SetForegroundColour(hex_c(DIM))
            txt.SetFont(self._font(8))
            cv.Add(txt, 0, wx.LEFT | wx.TOP, 12)
            c.sizer = cv
            return c

        def combo(parent, choices, value):
            cb = wx.ComboBox(parent, choices=choices, value=value,
                             size=(-1, 26), style=wx.CB_READONLY)
            cb.SetBackgroundColour(hex_c("#1e1f28"))
            cb.SetForegroundColour(hex_c(TXT))
            return cb

        def fcheck(parent, label, value, cb_fn):
            cbk = wx.CheckBox(parent, label=label)
            cbk.SetValue(value)
            cbk.SetForegroundColour(hex_c(TXT))
            cbk.SetFont(self._font(9))
            if cb_fn:
                cbk.Bind(wx.EVT_CHECKBOX, cb_fn)
            return cbk

        v = wx.BoxSizer(wx.VERTICAL)
        v.AddSpacer(10)

        # header
        header = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(panel, label="🎓 HSC Result Bot")
        title.SetForegroundColour(hex_c(TXT))
        title.SetFont(self._font(13, bold=True))
        header.Add(title, 0, wx.LEFT, 14)
        header.AddStretchSpacer()
        self.pill = wx.StaticText(panel, label="idle")
        self.pill.SetForegroundColour(hex_c(DIM))
        header.Add(self.pill, 0, wx.RIGHT, 14)
        v.Add(header, 0, wx.EXPAND)
        v.AddSpacer(8)

        # chrono card
        chrono = card("FIRES AT (the pc clock's zone)")
        self.clock = wx.StaticText(chrono, label="--:--:--")
        self.clock.SetForegroundColour(hex_c(TXT))
        self.clock.SetFont(self._font(28, mono=True, bold=True))
        chrono.sizer.Add(self.clock, 0, wx.ALIGN_CENTER | wx.TOP, 4)
        grow = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 4))
        grow.AddGrowableCol(1)
        self.f_date = wx.adv.DatePickerCtrl(
            chrono, size=(-1, 26),
            style=wx.adv.DP_DROPDOWN | wx.adv.DP_SHOWCENTURY)
        self.f_date.SetFont(self._font(9))
        self.f_time = wx.adv.TimePickerCtrl(chrono, size=(-1, 26))
        self.f_time.SetFont(self._font(9))
        self.f_date.SetToolTip("result day — fires that exact second")
        grow.Add(flabel(chrono, "Date"), 0, wx.ALIGN_CENTER_VERTICAL)
        grow.Add(self.f_date, 1, wx.EXPAND)
        grow.Add(flabel(chrono, "Time"), 0, wx.ALIGN_CENTER_VERTICAL)
        grow.Add(self.f_time, 1, wx.EXPAND)
        chrono.sizer.Add(grow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        chrono.SetSizer(chrono.sizer)
        v.Add(chrono, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        v.AddSpacer(8)

        # bot buttons
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.b_save = self._btn(panel, "Save", PANE, TXT)
        self.b_test = self._btn(panel, "Test", "#242531", "#c9cbff")
        self.b_run = self._btn(panel, "Run now", WARN, "#20222b")
        self.b_go = self._btn(panel, "Go", BLUE, "#ffffff")
        self.b_stop = self._btn(panel, "Stop", BADC, "#ffffff")
        for i, btn in enumerate([self.b_save, self.b_test, self.b_run,
                                 self.b_go, self.b_stop]):
            row.Add(btn, 1, wx.LEFT if i == 0 else 4)
        row.AddSpacer(14)
        self.b_save.Bind(wx.EVT_BUTTON, self.on_save)
        self.b_test.Bind(wx.EVT_BUTTON, self.on_test)
        self.b_run.Bind(wx.EVT_BUTTON, self.on_runnow)
        self.b_go.Bind(wx.EVT_BUTTON, self.on_go)
        self.b_stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.b_stop.Enable(False)
        v.Add(row, 0, wx.EXPAND | wx.LEFT, 14)
        v.AddSpacer(8)

        # queue card
        qc = card("QUEUE — priority top→down · synced to the gist "
                  "(serve/cloud read it live)")
        self.q_grid = QueueGrid(qc)
        qc.sizer.Add(self.q_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        gq = wx.FlexGridSizer(cols=6, gap=wx.Size(6, 4))
        gq.AddGrowableCol(0)
        self.m_name = finput(qc)
        self.m_name.SetValue("name…")
        self.m_exam = combo(qc, EXAMS, "hsc")
        self.m_board = combo(qc, BOARDS, "dhaka")
        self.m_year = finput(qc)
        self.m_year.SetValue("2026")
        self.m_roll = finput(qc)
        self.m_roll.SetHint("roll")
        self.m_reg = finput(qc)
        self.m_reg.SetHint("reg")
        for w in (self.m_name, self.m_exam, self.m_board, self.m_year,
                  self.m_roll, self.m_reg):
            gq.Add(w, 1, wx.EXPAND)
        qc.sizer.Add(gq, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        qrow = wx.BoxSizer(wx.HORIZONTAL)
        self.q_add = self._btn(qc, "＋ Add", BLUE, "#ffffff")
        self.q_up = self._btn(qc, "↑", PANE, TXT)
        self.q_dn = self._btn(qc, "↓", PANE, TXT)
        self.q_del = self._btn(qc, "✕ Delete", PANE, TXT)
        self.q_reload = self._btn(qc, "Reload", PANE, TXT)
        for i, b in enumerate([self.q_add, self.q_up, self.q_dn,
                               self.q_del, self.q_reload]):
            qrow.Add(b, 1, wx.LEFT if i == 0 else 4)
        qrow.AddSpacer(12)
        self.q_add.Bind(wx.EVT_BUTTON, self.q_on_add)
        self.q_up.Bind(wx.EVT_BUTTON, lambda e: self.q_move(-1))
        self.q_dn.Bind(wx.EVT_BUTTON, lambda e: self.q_move(1))
        self.q_del.Bind(wx.EVT_BUTTON, self.q_on_del)
        self.q_reload.Bind(wx.EVT_BUTTON, lambda e: self.queue_reload())
        qc.sizer.Add(qrow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        qc.SetSizer(qc.sizer)
        v.Add(qc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        v.AddSpacer(8)

        # student card (config.json single-student legacy — kept for main.py)
        card1 = card("CURRENT STUDENT (local runs)")
        g1 = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 6))
        g1.AddGrowableCol(1)
        self.f_exam = combo(card1, EXAMS, "hsc")
        self.f_board = combo(card1, BOARDS, "dhaka")
        self.f_year = finput(card1)
        self.f_roll = finput(card1)
        self.f_reg = finput(card1)
        for label, widget in [("Exam", self.f_exam), ("Board", self.f_board),
                              ("Year", self.f_year), ("Roll", self.f_roll),
                              ("Reg no.", self.f_reg)]:
            g1.Add(flabel(card1, label), 0, wx.ALIGN_CENTER_VERTICAL)
            g1.Add(widget, 1, wx.EXPAND)
        card1.sizer.Add(g1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        card1.SetSizer(card1.sizer)
        v.Add(card1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        v.AddSpacer(8)

        # telegram card
        card2 = card("TELEGRAM — @hscresultbot")
        g2 = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 6))
        g2.AddGrowableCol(1)
        self.f_token = finput(card2)
        self.f_chat = finput(card2)
        for label, widget in [("Bot token", self.f_token), ("Chat ID", self.f_chat)]:
            g2.Add(flabel(card2, label), 0, wx.ALIGN_CENTER_VERTICAL)
            g2.Add(widget, 1, wx.EXPAND)
        card2.sizer.Add(g2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        card2.SetSizer(card2.sizer)
        v.Add(card2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        v.AddSpacer(8)

        # pacing card
        pc = card("HOW OFTEN IT CHECKS (serve re-tunes ≤30s, no restart)")
        g5 = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 6))
        g5.AddGrowableCol(1)
        self.f_day_int = fnum(pc)
        self.f_night_int = fnum(pc)
        self.f_stagger = fnum(pc)
        self.f_timeout = fnum(pc)
        for label, widget in [("Day interval (s)", self.f_day_int),
                              ("Night interval (s)", self.f_night_int),
                              ("Mirror stagger (s)", self.f_stagger),
                              ("Request timeout (s)", self.f_timeout)]:
            g5.Add(flabel(pc, label), 0, wx.ALIGN_CENTER_VERTICAL)
            g5.Add(widget, 1, wx.EXPAND)
        pc.sizer.Add(g5, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        pc.SetSizer(pc.sizer)
        v.Add(pc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        v.AddSpacer(8)

        # advanced card
        ac = card("ADVANCED")
        arow = wx.BoxSizer(wx.HORIZONTAL)
        self._mirror_checks = []
        for key in ("educationboardresults_gov_bd", "eboardresults_com"):
            cbk = wx.CheckBox(ac, label=key.replace("_gov_bd", "").replace("_com", "")
                              + ".gov.bd" if key.endswith("_gov_bd")
                              else "eboardresults.com")
            cbk.SetForegroundColour(hex_c(TXT))
            arow.Add(cbk, 0, wx.LEFT, 4)
            arow.AddSpacer(10)
            self._mirror_checks.append((key, cbk))
        ac.sizer.Add(arow, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
        arow2 = wx.BoxSizer(wx.HORIZONTAL)
        self.f_gist = finput(ac)
        self.f_gist.SetHint("gist id for the shared queue")
        arow2.Add(flabel(ac, "Gist"), 0, wx.ALIGN_CENTER_VERTICAL)
        arow2.Add(self.f_gist, 1, wx.EXPAND | wx.RIGHT, 6)
        ac.sizer.Add(arow2, 1, wx.EXPAND | wx.LEFT, 6)
        self.f_autostart = fcheck(ac, "start serve on pc boot", False, None)
        ac.sizer.Add(self.f_autostart, 0, wx.LEFT | wx.BOTTOM, 8)
        ac.SetSizer(ac.sizer)
        v.Add(ac, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        v.AddSpacer(8)

        # serve control
        srow = wx.BoxSizer(wx.HORIZONTAL)
        self.serve_btn = self._btn(panel, "Serve: start", OKC, "#0c1a12")
        srow.Add(self.serve_btn, 1, wx.LEFT | wx.EXPAND, 14)
        srow.AddSpacer(14)
        self.serve_btn.Bind(wx.EVT_BUTTON, self.on_serve_toggle)
        v.Add(srow, 0, wx.EXPAND)
        v.AddSpacer(6)

        # log grid
        self.loggrid = LogGrid(panel)
        v.Add(self.loggrid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(v)

        for key, cbk in self._mirror_checks:
            cbk.Bind(wx.EVT_CHECKBOX, self.on_save)
        self.f_autostart.Bind(wx.EVT_CHECKBOX, self.on_autostart)

    def _btn(self, parent, label, bg, fg):
        b = wx.Button(parent, label=label, size=(72, 32))
        b.SetBackgroundColour(hex_c(bg))
        b.SetForegroundColour(hex_c(fg))
        b.SetFont(self._font(9, bold=True))
        return b

    # ----- helpers -------------------------------------------------------
    def log(self, tag: str, msg: str):
        self.loggrid.append(tag, msg)

    def _gist(self):
        """QueueStore lazily built from gh auth token (same as serve)."""
        if self.q_store:
            return self.q_store
        import json as _json
        cfg = load_config()
        gid = (cfg.get("gist") or {}).get("id") or GIST_ID_DEFAULT
        try:
            if self.f_gist.GetValue().strip():
                gid = self.f_gist.GetValue().strip()
        except Exception:
            pass
        tok = os.environ.get("GIST_TOKEN", "").strip()
        if not tok:
            r = subprocess.run(["gh", "auth", "token"],
                               capture_output=True, text=True, timeout=15)
            tok = (r.stdout or "").strip()
        if not tok:
            raise RuntimeError("no gist token — install gh or set GIST_TOKEN")
        self.q_store = QueueStore(tok, gid)
        return self.q_store

    def _apply_pacing(self, cfg):
        try:
            engine_cfg = cfg.setdefault("engine", {})
            engine_cfg["cycle_interval"] = int(self.f_day_int.GetValue() or 30)
            engine_cfg["night_interval"] = int(self.f_night_int.GetValue() or 300)
            engine_cfg["mirror_stagger"] = int(self.f_stagger.GetValue() or 5)
            cfg.setdefault("retry", {})["request_timeout_seconds"] = int(
                self.f_timeout.GetValue() or 20)
        except ValueError:
            self.log("err", "pacing values must be whole numbers")
        return cfg

    # ----- queue ----------------------------------------------------------
    def queue_reload(self):
        def work():
            with self._gist_lock:
                try:
                    store = self._gist()
                    rows = store.students()
                    wx.CallAfter(self._queue_loaded, rows)
                except Exception as e:
                    wx.CallAfter(self.log, "err",
                                 "queue load failed: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def _queue_loaded(self, rows):
        self.q_rows = rows
        self.q_grid.fill(rows)
        self.log("ok", "queue %d row(s) from gist" % len(rows))
        # mirror row 1 (priority) into local current-student fields
        if rows:
            r0 = rows[0]
            self.f_exam.SetValue(str(r0.get("exam", "hsc")))
            self.f_board.SetValue(str(r0.get("board", "dhaka")))
            self.f_year.SetValue(str(r0.get("year", "2026")))
            self.f_roll.SetValue(str(r0.get("roll", "")))
            self.f_reg.SetValue(str(r0.get("reg", "")))

    def _q_write(self, rows):
        def work():
            with self._gist_lock:
                try:
                    self._gist().save_students(rows)
                except Exception as e:
                    wx.CallAfter(self.log, "err", "queue save failed: %s" % e)
                    return
            wx.CallAfter(self.queue_reload)
        threading.Thread(target=work, daemon=True).start()

    def q_on_add(self, e):
        try:
            row = {
                "name": self.m_name.GetValue().strip() or "student-%d"
                        % (len(self.q_rows) + 1),
                "exam": self.m_exam.GetValue() or "hsc",
                "board": (self.m_board.GetValue() or "dhaka").lower(),
                "year": self.m_year.GetValue().strip() or "2026",
                "roll": self.m_roll.GetValue().strip(),
                "reg": self.m_reg.GetValue().strip(),
            }
        except Exception:
            row = None
        if not row or not row["roll"] or not row["reg"]:
            self.log("err", "roll + reg needed to add a student")
            return
        if any(str(x.get("roll")) == row["roll"] for x in self.q_rows):
            self.log("err", "roll %s already in queue" % row["roll"])
            return
        self. m_name.SetValue("name…")
        self.m_roll.SetValue("")
        self.m_reg.SetValue("")
        self._q_write(self.q_rows + [row])

    def _q_sel(self):
        i = self.q_grid.GetFirstSelected()
        return -1 if i < 0 else i

    def q_on_del(self, e):
        i = self._q_sel()
        if i < 0:
            self.log("err", "pick a row in the queue list first")
            return
        rows = self.q_rows[:i] + self.q_rows[i + 1:]
        self._q_write(rows)

    def q_move(self, delta):
        i = self._q_sel()
        if i < 0:
            self.log("err", "pick a row to move first")
            return
        j = 0 if delta < 0 else len(self.q_rows) - 1  # top → 0, bottom → end
        rows = list(self.q_rows)
        row = rows.pop(i)
        rows.insert(j, row)
        self._q_write(rows)

    def on_autostart(self, e):
        enabled = self.f_autostart.GetValue()
        try:
            src = os.path.join(BASE, "start_serve.bat")
            if enabled:
                os.makedirs(os.path.dirname(STARTUP_BAT), exist_ok=True)
                with open(STARTUP_BAT, "w", encoding="utf-8") as fh:
                    fh.write('@echo off\r\ncd /d "%s"\r\nstart "" pythonw serve.py\r\n'
                             % BASE)
                self.log("ok", "auto-start on pc boot: ON")
            elif os.path.exists(STARTUP_BAT):
                os.remove(STARTUP_BAT)
                self.log("pause", "auto-start on pc boot: OFF")
        except Exception as ex:
            self.log("err", "auto-start toggle failed: %s" % type(ex).__name__)
            self.f_autostart.SetValue(not enabled)

    def _refresh_serve_btn(self):
        live = self._serve_pid() is not None
        self.serve_btn.SetLabel(
            "Serve: RUNNING (click to stop)" if live else "Serve: start")
        self.serve_btn.SetBackgroundColour(hex_c(BADC if live else OKC))
        self.serve_btn.SetForegroundColour(
            hex_c("#ffffff" if live else "#0c1a12"))

    def _serve_pids(self):
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter "
                 "\"name='python.exe' or name='pythonw.exe'\" | "
                 "ForEach-Object { \"{0}|{1}\" -f $_.ProcessId,$_.CommandLine }"],
                capture_output=True, text=True, errors="replace", timeout=15,
            ).stdout or ""
        except Exception:
            return []
        pids = []
        for line in out.splitlines():
            if "serve.py" not in line or "|" not in line:
                continue
            pid = line.split("|", 1)[0].strip()
            if pid.isdigit() and int(pid) != os.getpid() \
                    and int(pid) not in pids:
                pids.append(int(pid))
        return pids

    def _serve_pid(self):
        pids = self._serve_pids()
        return pids[0] if pids else None

    def on_serve_toggle(self, e):
        pids = self._serve_pids()
        if pids:
            for pid in pids:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=15)
            self.log("pause", "serve stopped (%s)"
                     % ", ".join(str(p) for p in pids))
        else:
            cfg = load_config()
            self._apply_pacing(cfg)
            save_config(cfg)
            self._spawn_serve()
            self.log("info", "🟢 serve starting hidden — buttons live in ≤5s")
        wx.CallLater(2500, self._refresh_serve_btn)

    def _spawn_serve(self):
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        subprocess.Popen(
            [sys.executable, "serve.py"], cwd=BASE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creationflags, env=env)

    # ----- config form ----------------------------------------------------
    def _load_cfg_into_form(self):
        cfg = load_config()
        self.f_exam.SetValue(cfg["student"].get("examination", "hsc"))
        self.f_board.SetValue(cfg["student"].get("board", "dhaka"))
        self.f_year.SetValue(cfg["student"].get("year", "2026"))
        self.f_roll.SetValue(cfg["student"].get("roll", ""))
        self.f_reg.SetValue(cfg["student"].get("reg", ""))
        self.f_token.SetValue(cfg["telegram"].get("bot_token", ""))
        self.f_chat.SetValue(cfg["telegram"].get("chat_id", ""))
        eng = cfg.get("engine") or {}
        self.f_day_int.SetValue(str(eng.get("cycle_interval", 30)))
        self.f_night_int.SetValue(str(eng.get("night_interval", 300)))
        self.f_stagger.SetValue(str(eng.get("mirror_stagger", 5)))
        self.f_timeout.SetValue(str(cfg["retry"].get(
            "request_timeout_seconds", 20)))
        mirrors = cfg.get("mirrors", {})
        for key, cbk in self._mirror_checks:
            cbk.SetValue(bool((mirrors.get(key) or {}).get("enabled")))
        self.f_gist.SetValue((cfg.get("gist") or {}).get("id", ""))
        self.f_autostart.SetValue(os.path.exists(STARTUP_BAT))
        dt_iso = (cfg["schedule"].get("target_date") or "").strip()
        if dt_iso:
            try:
                d = datetime.date.fromisoformat(dt_iso)
                self.f_date.SetValue(wx.DateTime.FromDMY(
                    d.day, wx.DateTime.Month(int(d.month) - 1), d.year))
            except (ValueError, TypeError):
                pass
        else:
            self.f_date.SetValue(wx.DateTime.Now())
        sc = cfg["schedule"]
        self.f_time.SetValue(wx.DateTime.FromDMY(
            1, wx.DateTime.Month(0), 2026,
            int(sc.get("start_hour_local", 10)),
            int(sc.get("start_minute_local", 59)),
            int(sc.get("start_second_local", 55))))
        self._refresh_countdown()
        if not self.timer:
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, lambda e: self._tick(), self.timer)
            self.timer.Start(500)

    def _refresh_countdown(self):
        tgt = countdown_target(load_config(), force_now=False)
        try:
            dt = datetime.datetime.fromisoformat(tgt["iso"])
        except ValueError:
            return
        self.countdown_target_iso = dt

    def _tick(self):
        if not self.countdown_target_iso:
            return
        target = self.countdown_target_iso
        now = datetime.datetime.now(target.tzinfo) if target.tzinfo \
            else datetime.datetime.now()
        if target.tzinfo:
            target = target.replace(tzinfo=None)
        if now.tzinfo:
            now = now.replace(tzinfo=None)
        remain = (target - now).total_seconds()
        if remain <= 0:
            self.clock.SetLabel("firing")
            return
        days = int(remain // 86400)
        h = int(remain % 86400 // 3600)
        m = int(remain % 3600 // 60)
        s = int(remain % 60)
        if days > 0:
            self.clock.SetLabel("%dd %02d:%02d:%02d" % (days, h, m, s))
        else:
            self.clock.SetLabel("%02d:%02d:%02d" % (h, m, s))

    # ----- subprocess management -----------------------------------------
    def proc_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _spawn(self, args, label: str):
        if self.proc_alive():
            self.log("err", "⏳ %s already running — Stop it first" % label)
            return False
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        self.mode = label
        self.proc = subprocess.Popen(
            [sys.executable] + args, cwd=BASE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=creationflags,
        )
        threading.Thread(target=self._pump, args=(self.proc,),
                         daemon=True).start()
        return True

    def _pump(self, proc):
        tagmap = ("ok", "err", "wait", "pause", "info")
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tag, msg = "info", line
            if line.startswith("[") and "]" in line[1:8]:
                close = line.index("]")
                maybe = line[1:close]
                if maybe in tagmap:
                    tag, msg = maybe, line[close + 2:]
            wx.CallAfter(self.log, tag, msg)
        code = proc.wait()
        wx.CallAfter(self._proc_done, code)

    def _proc_done(self, code: int):
        self.proc = None
        self.pill.SetLabel("idle")
        self.pill.SetForegroundColour(hex_c(DIM))
        self.b_go.Enable(True)
        self.b_stop.Enable(False)
        self.b_test.Enable(True)
        if code == 0:
            if self.mode == "test":
                self.log("ok", "✅ test finished")
            else:
                self.log("ok", "✅ bot exited — result delivered")
        else:
            self.log("err", "🛑 %s exited (code %d)" % (self.mode, code))

    # ----- buttons -------------------------------------------------------
    def on_save(self, e):
        cfg = load_config()
        cfg["student"]["examination"] = self.f_exam.GetValue()
        cfg["student"]["board"] = self.f_board.GetValue()
        cfg["student"]["year"] = self.f_year.GetValue()
        cfg["student"]["roll"] = self.f_roll.GetValue()
        cfg["student"]["reg"] = self.f_reg.GetValue()
        cfg["telegram"]["bot_token"] = self.f_token.GetValue()
        cfg["telegram"]["chat_id"] = self.f_chat.GetValue()
        dt = self.f_date.GetValue()
        if dt.IsValid():
            cfg["schedule"]["target_date"] = "%04d-%02d-%02d" % (
                dt.GetYear(), dt.GetMonth() + 1, dt.GetDay())
        else:
            cfg["schedule"]["target_date"] = ""
        tm = self.f_time.GetValue()
        cfg["schedule"]["start_hour_local"] = tm.GetHour()
        cfg["schedule"]["start_minute_local"] = tm.GetMinute()
        cfg["schedule"]["start_second_local"] = tm.GetSecond()
        mirrors = cfg.setdefault("mirrors", {})
        for key, cbk in self._mirror_checks:
            mirrors.setdefault(key, {})["enabled"] = bool(cbk.GetValue())
        cfg.setdefault("gist", {})["id"] = self.f_gist.GetValue().strip() \
            or GIST_ID_DEFAULT
        self._apply_pacing(cfg)
        save_config(cfg)
        self.log("ok", "💾 config saved — serve re-tunes within 30s")
        problems = validate(cfg)
        if problems:
            self.log("err", "still missing: %s" % "; ".join(problems))
        else:
            self.log("ok", "config complete")
        self._refresh_countdown()

    def on_test(self, e):
        if self.proc_alive():
            self.log("err", "⏳ bot already running — Stop it first")
            return
        self.log("info", "🧪 pinging mirrors + telegram…")
        self.on_save(None)
        if self._spawn(["main.py", "--test"], "test"):
            self.b_test.Enable(False)
            self.pill.SetLabel("testing")
            self.pill.SetForegroundColour(hex_c(BLUE))

    def on_runnow(self, e):
        if self.proc_alive():
            self.log("err", "⏳ bot already running — Stop it first")
            return
        self.on_save(None)
        if self._spawn(["main.py", "--force-now"], "run-now"):
            self.pill.SetLabel("dry-run")
            self.pill.SetForegroundColour(hex_c(WARN))
            self.b_go.Enable(False)
            self.b_stop.Enable(True)
            self.log("info", "🔥 run-now — pulls ignoring the countdown")

    def on_go(self, e):
        if self.proc_alive():
            self.log("err", "⏳ bot already running — Stop it first")
            return
        self.on_save(None)
        if self._spawn(["main.py"], "bot"):
            self.pill.SetLabel("running")
            self.pill.SetForegroundColour(hex_c(OKC))
            self.b_go.Enable(False)
            self.b_stop.Enable(True)
            self.log("info", "🚀 started — countdown armed in subprocess")

    def on_stop(self, e):
        if not self.proc_alive():
            self.log("pause", "nothing running")
            return
        self.proc.terminate()
        self.log("pause", "🛑 stopping…")

    def on_close(self, e):
        if self.proc_alive():
            self.proc.terminate()
        e.Skip()


class App(wx.App):
    def OnInit(self):
        f = MainFrame()
        f.Show()
        f.Bind(wx.EVT_CLOSE, f.on_close)
        return True


if __name__ == "__main__":
    App(False).MainLoop()
