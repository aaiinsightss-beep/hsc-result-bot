# -*- coding: utf-8 -*-
"""Telegram long-poll cockpit — buttons answer while the brain is awake.

Daemon thread inside brain.py. The brain polls the queue top-down in its
own thread; both meet on the shared QueueStore. Commands:
  /start /help            — button menu
  /add name board year roll reg [exam] — add a student (index = end)
  /list                   — numbered queue, found rows marked
  /del N                  — remove row N
  /top N                  — move row N to index 1
  /run                    — poll once right now, reply the verdict
  /stop                   — cancel polling for this window
  /status                 — engine state, mirrors, next wake
Inline buttons send the same commands as callback payloads. Only the
configured chat gets answers; strangers are ignored.
"""
import datetime
import threading
import time

import requests

API = "https://api.telegram.org/bot{token}/{method}"

BOARDS = ["dhaka", "comilla", "chittagong", "rajshahi", "jessore", "khulna",
          "barishal", "sylhet", "dinajpur", "mymensingh", "madrasah",
          "technical"]
EXAMS = ("ssc", "hsc", "dakhil", "voc")

MENU_TEXT = (
    "🎓 hsc-bot cockpit\n\n"
    "/add name board year roll reg [exam]\n"
    "/list · /del N · /top N\n"
    "/run — poll once right now\n"
    "/stop — cancel this window\n"
    "/status — engine, mirrors, next wake"
)
MENU_KB = {"inline_keyboard": [
    [{"text": "▶️ Run now", "callback_data": "/run"},
     {"text": "🧭 Status", "callback_data": "/status"}],
    [{"text": "📋 List students", "callback_data": "/list"},
     {"text": "🛑 Stop", "callback_data": "/stop"}],
]}


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
        body = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if kb:
            body["reply_markup"] = kb
        if not self._api("sendMessage", **body):
            body.pop("reply_markup", None)
            body.pop("parse_mode", None)
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
            self.send(MENU_TEXT, MENU_KB)
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
            except requests.RequestException:
                time.sleep(5)
                continue
            except Exception:
                time.sleep(5)
                continue
            if updates:
                self._offset = int(updates[-1]["update_id"]) + 1
                # gist_update pool is 100 writes/hour — persist the offset
                # at most every ~2 min (memory keeps it exact meanwhile)
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

    def _safe_handle(self, update: dict) -> None:
        try:
            self._handle(update)
        except Exception as e:
            try:
                self.send("⚠️ cockpit hiccup: %s" % type(e).__name__)
            except Exception:
                pass

    # -- dispatch ----------------------------------------------------------
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
            self._command(_std_text(text), msg.get("from") or {})

    def _command(self, cmd: str, from_user: dict) -> None:
        head = cmd.split(" ", 1)[0].lower()
        argline = cmd[len(cmd.split(" ", 1)[0]):].strip()
        try:
            (self._c_start if head in ("/start", "/help") else
             self._c_add if head == "/add" else
             self._c_list if head == "/list" else
             self._c_del if head == "/del" else
             self._c_top if head == "/top" else
             self._c_run if head == "/run" else
             self._c_stop if head == "/stop" else
             self._c_status if head == "/status" else
             self._c_unknown)(argline)
        except Exception as e:
            self.send("⚠️ %s failed: %s" % (head, type(e).__name__))

    # -- commands ----------------------------------------------------------
    def _c_unknown(self, _) -> None:
        self.send("unknown command — /help", MENU_KB)

    def _c_start(self, _) -> None:
        self.send(MENU_TEXT, MENU_KB)

    _c_help = _c_start

    def _c_add(self, args: str) -> None:
        parts = [p for p in args.split() if p]
        if len(parts) not in (5, 6):
            raise ValueError("need: name board year roll reg [exam]")
        name, board, year, roll, reg = parts[:5]
        exam = (parts[5].lower() if len(parts) == 6 else "ssc")
        if board.lower() not in BOARDS:
            raise ValueError("board? %s" % ", ".join(BOARDS))
        if exam not in EXAMS:
            raise ValueError("exam? %s" % ", ".join(EXAMS))
        if not (year.isdigit() and len(year) == 4):
            raise ValueError("year needs 4 digits")
        if not roll.isdigit() or not reg.isdigit():
            raise ValueError("roll / reg need digits")
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
        self.send("➕ %s added at index %d — queue %d row(s)"
                  % (name, len(students), len(students)))

    def _c_list(self, _) -> None:
        students = self.store.students()
        if not students:
            self.send("queue empty — /add to load one in")
            return
        lines = ["📋 queue"]
        for i, r in enumerate(students, 1):
            mark = "✅ %s" % r["found_via"] if r.get("found_at") else "⏳"
            lines.append("%d. %s — %s %s roll %s · %s %s"
                         % (i, r.get("name", "?"), r.get("board", "?"),
                            r.get("year", "?"), r.get("roll", "?"),
                            r.get("exam", "ssc"), mark))
        self.send("\n".join(lines))

    def _c_del(self, args: str) -> None:
        n = int(args.split()[0]) if args.split() and args.split()[0].isdigit() else 0

        def drop(st):
            if not (1 <= n <= len(st)):
                raise ValueError("no row %d — queue holds %d" % (n, len(st)))
            gone = st[n - 1]
            self._del_note = "%d. %s" % (n, gone.get("name", "?"))
            return [r for i, r in enumerate(st, 1) if i != n]

        students = self.store.mutate_students(drop)
        self.send("🗑 removed %s — queue now %d row(s)"
                  % (self._del_note, len(students)))

    def _c_top(self, args: str) -> None:
        n = int(args.split()[0]) if args.split() and args.split()[0].isdigit() else 0
        students = self.store.students()
        if not (1 <= n <= len(students)):
            raise ValueError("no row %d — queue holds %d" % (n, len(students)))
        if n == 1:
            self.send("1. %s already tops the queue" % students[0].get("name", "?"))
            return

        def move(rows):
            row = rows.pop(n - 1)
            row["found_at"] = ""          # re-promote re-enters the grind
            row["found_via"] = ""
            return [row] + rows

        students = self.store.mutate_students(move)
        self.send("🔝 %s now polls first (queue %d row(s))"
                  % (students[0].get("name", "?"), len(students)))

    def _c_run(self, _) -> None:
        if self.run_grind:
            ok = False
            try:
                ok = self.run_grind()
            except Exception as e:
                self.send("⚠️ dispatch failed: %s" % type(e).__name__)
                return
            self.send("🔥 full pass firing — verdict in ~2 min"
                      if ok else
                      "⚠️ could not trigger the grind — Actions API said no")
            return
        self.run_once.set()
        self.send("🔬 polling one pass right now — verdict in a minute…")

    def _c_stop(self, _) -> None:
        today = datetime.datetime.now().isoformat()[:10]
        self.store.update_state(polling=False, stop_date=today)
        self.hard_stop.set()
        self.send("🛑 window closed — polling stops; tomorrow's cron re-opens it")

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
