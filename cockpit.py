# -*- coding: utf-8 -*-
"""Telegram long-poll cockpit — buttons answer while the brain is awake.

Daemon thread inside brain.py/serve.py. Polls the queue top-down on the
shared QueueStore. Commands:
  /start /help /menu      — the menu (also a persistent reply keyboard)
  /add name board year roll reg [exam]
  /list                   — numbered queue + GPA for landed rows
  /del N                  — remove row N (no arg = show the queue)
  /top N                  — move row N to the front (re-enters the grind)
  /result                 — re-send stored result card(s), no re-poll
  /run                    — poll once right now, reply the verdict
  /stop                   — cancel polling for this window
  /status                 — engine state, mirrors, next wake

The reply keyboard (menu bar) is attached to every outgoing message, so
it stays pinned above the input field with zero effort. Button presses
arrive as plain texts and are aliased to commands. Only the configured
chat gets answers; strangers are ignored.
"""
import datetime
import threading
import time

import requests

import notifier

API = "https://api.telegram.org/bot{token}/{method}"

BOARDS = ["dhaka", "comilla", "chittagong", "rajshahi", "jessore", "khulna",
          "barishal", "sylhet", "dinajpur", "mymensingh", "madrasah",
          "technical"]
EXAMS = ("ssc", "hsc", "dakhil", "voc")

MENU_TEXT = (
    "🎓 hsc-bot cockpit\n\n"
    "/add name board year roll reg [exam]\n"
    "/list · /del N · /top N\n"
    "/result — send the result card again\n"
    "/run — poll once right now\n"
    "/stop — cancel this window\n"
    "/status — engine, mirrors, next wake"
)

# persistent reply keyboard — the always-there menu bar
MENU_REPLY = {"keyboard": [
    ["▶️ Run now", "🧭 Status"],
    ["📋 List students", "📑 Results"],
    ["🛑 Stop"],
], "resize_keyboard": True, "is_persistent": True,
   "input_field_placeholder": "menu…"}

# keyboard button text → command (exact, lowercase compare)
_BTN_ALIAS = {
    "▶️ run now": "/run",
    "🧭 status": "/status",
    "📋 list students": "/list",
    "📑 results": "/result",
    "🛑 stop": "/stop",
}


def _std_text(raw: str) -> str:
    """Telegram-wide leading /command — tolerate @botname tails."""
    return raw.split("@", 1)[0].strip()


class Cockpit(threading.Thread):
    """Runs until shutdown_event; never leaks an exception to the brain."""

    def __init__(self, token: str, chat_id: str, store,
                 announce: bool = True, run_grind=None):
        super().__init__(daemon=True, name="cockpit")
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.store = store
        self.announce = announce          # sweep runs stay silent on boot
        self.run_grind = run_grind        # sweep: fire a real grind via API
        self.run_once = threading.Event()    # /run — one immediate pass
        self.hard_stop = threading.Event()   # /stop — end the window
        self.shutdown_event = threading.Event()
        self._offset = 0
        self._s = requests.Session()

    # -- aviation ----------------------------------------------------------
    def _api(self, method: str, **body):
        r = self._s.post(API.format(token=self.token, method=method),
                         json=body, timeout=35)
        try:
            return bool(r.ok and r.json().get("ok"))
        except ValueError:
            return False

    def send(self, text: str, kb=None) -> None:
        """Every message rides the persistent menu — it never disappears."""
        body = {"chat_id": self.chat_id, "text": text,
                "disable_web_page_preview": True,
                "reply_markup": kb or MENU_REPLY}
        if not self._api("sendMessage", **body):
            # strip markup on rejection — flag chat or odd text
            body.pop("reply_markup", None)
            body["text"] = text.replace("*", "").replace("`", "")
            self._api("sendMessage", **body)

    def answer(self, cb_id: str, text: str = "") -> None:
        self._api("answerCallbackQuery", callback_query_id=cb_id, text=text)

    # -- thread ------------------------------------------------------------
    def run(self) -> None:
        try:
            self._offset = int(self.store.state().get("tg_offset") or 0)
        except Exception:
            self._offset = 0
        if self.announce:
            self.send(MENU_TEXT)
        while not self.shutdown_event.is_set():
            try:
                r = self._s.get(
                    API.format(token=self.token, method="getUpdates"),
                    params={"timeout": 25, "offset": self._offset},
                    timeout=35,
                )
                if r.status_code == 409:   # a previous poller still lingering
                    time.sleep(3)
                    continue
                r.raise_for_status()
                updates = (r.json() or {}).get("result") or []
            except Exception:
                time.sleep(5)
                continue
            if updates:
                self._offset = int(updates[-1]["update_id"]) + 1
                # gist write pool is 100/hr — persist offset at most ~2min
                if time.time() - getattr(self, "_offset_ts", 0) > 121:
                    self._offset_ts = time.time()
                    try:
                        self.store.update_state(tg_offset=self._offset)
                    except Exception:
                        pass
            for u in updates:
                self._safe_handle(u)

    def stop(self) -> None:
        self.shutdown_event.set()

    # -- dispatch ----------------------------------------------------------
    def _safe_handle(self, update: dict) -> None:
        try:
            self._handle(update)
        except Exception as e:
            try:
                self.send("⚠️ cockpit hiccup: %s" % e)
            except Exception:
                pass

    def _handle(self, update: dict) -> None:
        cb = update.get("callback_query")
        if cb:
            data = _std_text(str(cb.get("data") or ""))
            self.answer(cb.get("id"), data)
            self._command(data, cb.get("from") or {})
            return
        msg = update.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        if chat != self.chat_id:
            return  # strangers stay ignored — no reply surface at all
        text = (msg.get("text") or "").strip()
        if text:
            alias = _BTN_ALIAS.get(text.lower())
            self._command(_std_text(alias or text), msg.get("from") or {})

    def _command(self, cmd: str, from_user: dict) -> None:
        head = cmd.split(" ", 1)[0].lower()
        argline = cmd[len(cmd.split(" ", 1)[0]):].strip()
        try:
            (self._c_start if head in ("/start", "/help", "/menu") else
             self._c_add if head == "/add" else
             self._c_list if head == "/list" else
             self._c_del if head == "/del" else
             self._c_top if head == "/top" else
             self._c_run if head == "/run" else
             self._c_result if head == "/result" else
             self._c_stop if head == "/stop" else
             self._c_status if head == "/status" else
             self._c_unknown)(argline)
        except Exception as e:
            # ValueError payloads are the human-readable hint text
            self.send("⚠️ %s — %s" % (head, e))

    # -- commands ----------------------------------------------------------
    def _c_unknown(self, _) -> None:
        self.send("that one's not on the menu — /help", MENU_REPLY)

    def _c_start(self, _) -> None:
        self.send(MENU_TEXT)

    _c_help = _c_start
    _c_menu = _c_start

    def _c_add(self, args: str) -> None:
        parts = [p for p in args.split() if p]
        if len(parts) not in (5, 6):
            self.send("usage: /add name board year roll reg [exam]\n"
                      "boards: %s" % ", ".join(BOARDS))
            return
        name, board, year, roll, reg = parts[:5]
        exam = (parts[5].lower() if len(parts) == 6 else "ssc")
        if board.lower() not in BOARDS:
            raise ValueError("board? %s" % ", ".join(BOARDS))
        if exam not in EXAMS:
            raise ValueError("exam? %s" % ", ".join(EXAMS))
        if not (year.isdigit() and len(year) == 4):
            raise ValueError("year needs 4 digits, got %r" % year)
        if not roll.isdigit() or not reg.isdigit():
            raise ValueError("roll / reg need digits, got %s / %s"
                             % (roll, reg))
        row = {
            "name": name, "board": board.lower(), "year": year, "exam": exam,
            "roll": roll, "reg": reg,
            "added_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "found_at": "", "found_via": "",
        }

        def append(st):
            if any(str(x.get("roll")) == roll for x in st):
                raise ValueError("roll %s already in queue" % roll)
            return st + [row]

        students = self.store.mutate_students(append)
        self.send("➕ %s added at %d of %d — result day fires it"
                  % (name, len(students), len(students)))

    def _c_list(self, _) -> None:
        students = self.store.students()
        if not students:
            self.send("queue empty — /add to load one in")
            return
        lines = ["📋 queue"]
        for i, r in enumerate(students, 1):
            if r.get("delivered_at"):
                mark = "✅ %s" % (r.get("found_via") or "delivered")
            elif r.get("found_at"):
                mark = "📨 found, card retrying"
            else:
                mark = "⏳ not out yet"
            gpa = (r.get("result") or {}).get("gpa")
            gpa_tail = " · GPA %s" % gpa if gpa else ""
            lines.append("%d. %s — %s %s roll %s · %s %s%s"
                         % (i, r.get("name", "?"), r.get("board", "?"),
                            r.get("year", "?"), r.get("roll", "?"),
                            r.get("exam", "ssc"), mark, gpa_tail))
        lines.append("🗑 /del N · 🔝 /top N · 📑 /result")
        self.send("\n".join(lines))

    def _c_del(self, args: str) -> None:
        parts = args.split()
        if not parts or not parts[0].isdigit():
            self._c_list(None)
            self.send("🗑 pick a row number: /del 2")
            return
        n = int(parts[0])

        def drop(st):
            if not (1 <= n <= len(st)):
                raise ValueError("no row %d — queue holds %d"
                                 % (n, max(1, len(st))))
            gone = st[n - 1]
            self._del_note = "%d. %s" % (n, gone.get("name", "?"))
            return [r for i, r in enumerate(st, 1) if i != n]

        students = self.store.mutate_students(drop)
        self.send("🗑 removed %s — queue now %d row(s)"
                  % (self._del_note, len(students)))

    def _c_top(self, args: str) -> None:
        parts = args.split()
        if not parts or not parts[0].isdigit():
            self._c_list(None)
            self.send("🔝 pick a row number: /top 2 (goes to first)")
            return
        n = int(parts[0])

        def move(st):
            if not (1 <= n <= len(st)):
                raise ValueError("no row %d — queue holds %d"
                                 % (n, max(1, len(st))))
            if n == 1:
                self._top_note = "%s already first" % \
                    (st[0].get("name", "?") or "row 1")
                return st
            row = st.pop(n - 1)
            row["found_at"] = ""          # re-promote re-enters the grind
            row["found_via"] = ""
            row["delivered_at"] = ""
            self._top_note = "%s" % row.get("name", "?")
            return [row] + st

        students = self.store.mutate_students(move)
        self.send("🔝 %s polls first now (%d row(s))"
                  % (self._top_note, len(students)))

    def _c_result(self, _) -> None:
        """Re-send stored result card(s) — instant, no board visit."""
        rows = self.store.students(fresh=True)
        landed = [r for r in rows if r.get("result")]
        if not landed:
            self.send("no stored results yet — fire /run once "
                      "(rows that landed already keep their card)")
            return
        sent = 0
        for r in landed:
            cfg = {"telegram": {"bot_token": self.token,
                                "chat_id": self.chat_id},
                   "student": {"examination": r.get("exam") or "ssc"}}
            try:
                self.send(notifier.format_result(cfg, r.get("result") or {}))
                sent += 1
            except Exception as e:
                self.send("⚠️ card for %s refused: %s"
                          % (r.get("name", "?"), e))
        if not sent:
            self.send("⚠️ every card refused — check chat_id/token")

    def _c_run(self, _) -> None:
        if self.run_grind:
            ok = False
            try:
                ok = self.run_grind()
            except Exception as e:
                self.send("⚠️ dispatch failed: %s" % e)
                return
            self.send("🔥 full pass firing — verdict in ~2 min"
                      if ok else
                      "⚠️ could not trigger the grind — Actions API said no")
            return
        if self.run_once.is_set():
            self.send("⏳ already on it — verdict landing in a moment…")
            return
        self.run_once.set()
        self.send("🔬 polling one pass right now — verdict in a minute…")

    def _c_stop(self, _) -> None:
        today = datetime.datetime.now().isoformat()[:10]
        self.store.update_state(polling=False, stop_date=today)
        self.hard_stop.set()
        self.send("🛑 window closed — polling stops; tomorrow's cron "
                  "re-opens it (or /run pushes one pass anyway)")

    def _c_status(self, _) -> None:
        st = self.store.state()
        status = st.get("status") or {}
        mir = status.get("mirrors") or {}
        last = status.get("last_cycle_ts") or 0
        ago = "%ds ago" % max(0, int(time.time() - last)) if last else "never"
        mirror_line = "\n".join(
            "• %s: %s" % (k, v) for k, v in sorted(mir.items())) or "• none"
        self.send(
            "🧭 status\n"
            "mode: %s · polling: %s\n"
            "students: %s — found: %s\n"
            "%s\n"
            "last cycle %s · next auto-cycle in %ss\n"
            "wake %s Asia/Dhaka"
            % (status.get("mode", "?"), st.get("polling", True),
               status.get("students", "?"), status.get("found", "?"),
               mirror_line, ago, status.get("interval", 30),
               status.get("wake", "?")))
