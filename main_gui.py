# -*- coding: utf-8 -*-
"""wxPython GUI — dark panel, countdown, log stream, run controls.

Architecture note (Win + wxPython): network lives in a main.py subprocess.
wx native event loops crash on TLS work in sibling threads (access violation
in urllib3), so this process does pipe reads only — wx-safe.
Fires:  python main_gui.py
"""
import datetime
import os
import subprocess
import sys
import threading
import wx
import wx.adv

from engine import load_config, save_config, countdown_target, validate

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


def hex_c(s: str) -> wx.Colour:
    return wx.Colour(s)


class LogGrid(wx.ListCtrl):
    """Plain (non-virtual) list — per-row colours, MSW-safe append wisdom.
    The LC_VIRTUAL machinery aborts natively on Win+wx4.3 colour attrs;
    log volume is tiny, so plain rows + SetItemTextColour is the right tool.
    """

    def __init__(self, parent):
        super().__init__(parent, style=wx.LC_REPORT)
        self.tag_col = {
            "ok": OKC, "err": BADC, "wait": WARN, "pause": WARN,
            "info": DIM, "cf": BLUE,
        }
        self.InsertColumn(0, "time", width=70)
        self.InsertColumn(1, "event", width=280)
        self.SetBackgroundColour(hex_c("#0a0b0f"))
        self.SetForegroundColour(hex_c(DIM))

    def append(self, tag: str, msg: str):
        idx = self.InsertItem(self.GetItemCount(), datetime.datetime.now().strftime("%H:%M:%S"))
        self.SetItem(idx, 1, msg)
        self.SetItemTextColour(idx, hex_c(self.tag_col.get(tag, DIM)))
        self.EnsureVisible(idx)
        # keep the log bounded — nobody wants 10k rows of retry noise
        if self.GetItemCount() > 400:
            self.DeleteItem(0)


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(
            None, title="HSC Bot", size=(430, 745),
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )
        self.SetBackgroundColour(hex_c(DARK))
        self.proc = None
        self.timer = None
        self.countdown_target_iso = None
        self._build_ui()
        self._load_cfg_into_form()

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

        v = wx.BoxSizer(wx.VERTICAL)
        v.AddSpacer(14)

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
        v.AddSpacer(10)

        # chrono card
        chrono = wx.Panel(panel)
        chrono.SetBackgroundColour(hex_c(PANE))
        cv = wx.BoxSizer(wx.VERTICAL)
        lbl = wx.StaticText(chrono, label="FIRES AT (Asia/Dhaka)")
        lbl.SetForegroundColour(hex_c(DIM))
        lbl.SetFont(self._font(8))
        cv.Add(lbl, 0, wx.ALIGN_CENTER | wx.TOP, 10)
        self.clock = wx.StaticText(chrono, label="--:--:--")
        self.clock.SetForegroundColour(hex_c(TXT))
        self.clock.SetFont(self._font(32, mono=True, bold=True))
        cv.Add(self.clock, 0, wx.ALIGN_CENTER | wx.TOP, 4)

        # target-date picker rows — Date + Time, native MSW pickers
        grow = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 4))
        grow.AddGrowableCol(1)
        self.f_date = wx.adv.DatePickerCtrl(
            chrono, size=(-1, 26),
            style=wx.adv.DP_DROPDOWN | wx.adv.DP_SHOWCENTURY,
        )
        self.f_date.SetFont(self._font(9))
        self.f_time = wx.adv.TimePickerCtrl(chrono, size=(-1, 26))
        self.f_time.SetFont(self._font(9))
        self.f_date.SetToolTip("result day — fires at the picked date+time")
        grow.Add(flabel(chrono, "Date"), 0, wx.ALIGN_CENTER_VERTICAL)
        grow.Add(self.f_date, 1, wx.EXPAND)
        grow.Add(flabel(chrono, "Time"), 0, wx.ALIGN_CENTER_VERTICAL)
        grow.Add(self.f_time, 1, wx.EXPAND)
        cv.Add(grow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        chrono.SetSizer(cv)
        v.Add(chrono, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        v.AddSpacer(10)

        # buttons
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.b_save = self._btn(panel, "Save", PANE, TXT)
        self.b_test = self._btn(panel, "Test", "#242531", "#c9cbff")
        self.b_run = self._btn(panel, "Run now", WARN, "#20222b")
        self.b_go = self._btn(panel, "Go", BLUE, "#ffffff")
        self.b_stop = self._btn(panel, "Stop", BADC, "#ffffff")
        for i, btn in enumerate([self.b_save, self.b_test, self.b_run, self.b_go, self.b_stop]):
            row.Add(btn, 1, wx.LEFT if i == 0 else 4)
        row.AddSpacer(14)
        self.b_save.Bind(wx.EVT_BUTTON, self.on_save)
        self.b_test.Bind(wx.EVT_BUTTON, self.on_test)
        self.b_run.Bind(wx.EVT_BUTTON, self.on_runnow)
        self.b_go.Bind(wx.EVT_BUTTON, self.on_go)
        self.b_stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.b_stop.Enable(False)
        v.Add(row, 0, wx.EXPAND | wx.LEFT, 14)
        v.AddSpacer(10)

        # student card
        card1 = wx.Panel(panel)
        card1.SetBackgroundColour(hex_c(PANE))
        g1 = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 8))
        g1.AddGrowableCol(1)

        self.f_exam = wx.ComboBox(card1, choices=["hsc", "ssc", "jsc"], value="hsc",
                                  size=(-1, 26), style=wx.CB_READONLY)
        self.f_exam.SetBackgroundColour(hex_c("#1e1f28"))
        self.f_exam.SetForegroundColour(hex_c(TXT))
        self.f_board = wx.ComboBox(card1, choices=BOARDS, value="dhaka",
                                   size=(-1, 26), style=wx.CB_READONLY)
        self.f_board.SetBackgroundColour(hex_c("#1e1f28"))
        self.f_board.SetForegroundColour(hex_c(TXT))
        self.f_year = finput(card1)
        self.f_roll = finput(card1)
        self.f_reg = finput(card1)
        for label, widget in [("Exam", self.f_exam), ("Board", self.f_board),
                              ("Year", self.f_year), ("Roll", self.f_roll),
                              ("Reg no.", self.f_reg)]:
            g1.Add(flabel(card1, label), 0, wx.ALIGN_CENTER_VERTICAL)
            g1.Add(widget, 1, wx.EXPAND)
        g1.AddSpacer(0); g1.AddSpacer(0)
        card1.SetSizer(g1, True)
        v.Add(card1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        v.AddSpacer(10)

        # telegram card
        card2 = wx.Panel(panel)
        card2.SetBackgroundColour(hex_c(PANE))
        g2 = wx.FlexGridSizer(cols=2, gap=wx.Size(10, 8))
        g2.AddGrowableCol(1)
        self.f_token = finput(card2)
        self.f_chat = finput(card2)
        for label, widget in [("Bot token", self.f_token), ("Chat ID", self.f_chat)]:
            g2.Add(flabel(card2, label), 0, wx.ALIGN_CENTER_VERTICAL)
            g2.Add(widget, 1, wx.EXPAND)
        g2.AddSpacer(0); g2.AddSpacer(0)
        card2.SetSizer(g2, True)
        v.Add(card2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)
        v.AddSpacer(10)

        # log grid
        self.loggrid = LogGrid(panel)
        v.Add(self.loggrid, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        panel.SetSizer(v)

    def _btn(self, parent, label, bg, fg):
        b = wx.Button(parent, label=label, size=(80, 34))
        b.SetBackgroundColour(hex_c(bg))
        b.SetForegroundColour(hex_c(fg))
        b.SetFont(self._font(10, bold=True))
        return b

    # ----- helpers -------------------------------------------------------
    def log(self, tag: str, msg: str):
        self.loggrid.append(tag, msg)

    def _load_cfg_into_form(self):
        cfg = load_config()
        self.f_exam.SetValue(cfg["student"].get("examination", "hsc"))
        self.f_board.SetValue(cfg["student"].get("board", "dhaka"))
        self.f_year.SetValue(cfg["student"].get("year", "2026"))
        self.f_roll.SetValue(cfg["student"].get("roll", ""))
        self.f_reg.SetValue(cfg["student"].get("reg", ""))
        self.f_token.SetValue(cfg["telegram"].get("bot_token", ""))
        self.f_chat.SetValue(cfg["telegram"].get("chat_id", ""))
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
        hh = int(sc.get("start_hour_local", 10))
        mm = int(sc.get("start_minute_local", 59))
        ss = int(sc.get("start_second_local", 55))
        self.f_time.SetValue(wx.DateTime.FromDMY(
            1, wx.DateTime.Month(0), 2026, hh, mm, ss))
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
        h = int(remain % 86400 // 3600); m = int(remain % 3600 // 60); s = int(remain % 60)
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
        self.mode = label
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # emoji in engine logs die on Windows locale pipes without this
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [sys.executable] + args, cwd=BASE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=creationflags,
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return True

    def _pump(self, proc):
        """Pipe-only thread — never touches TLS, wx-safe on Windows."""
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
        save_config(cfg)
        self.log("ok", "💾 config saved")
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
        self.on_save(None)  # form state -> config.json before any spawn
        if self._spawn(["main.py", "--test"], "test"):
            self.b_test.Enable(False)
            self.pill.SetLabel("testing")
            self.pill.SetForegroundColour(hex_c(BLUE))

    def on_runnow(self, e):
        """Dry-run — pulls immediately, ignoring the schedule (archives/2025 test)."""
        if self.proc_alive():
            self.log("err", "⏳ bot already running — Stop it first")
            return
        self.on_save(None)  # form state -> config.json before any spawn
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
        self.on_save(None)  # form state -> config.json before any spawn
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
        self.proc.terminate()  # Windows: TerminateProcess — hard stop is fine here
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
