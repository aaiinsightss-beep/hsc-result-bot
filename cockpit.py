# -*- coding: utf-8 -*-
"""Telegram long-poll cockpit — thumb-first, no command syntax needed.

Two ways to do everything:
  1) the wizard: tap ➕ Add student → bot asks one small question at a
     time, boards/exam/year picked from buttons, roll/reg typed,
     ✅ Save commits
  2) the old commands still work for power users (/add, /del N, /top N)

Priority management: the list message carries inline buttons —
⬆ ⬇ move a row, ⭐ make it first, 🗑 delete — the message edits in place
so the chat never floods.

The persistent reply keyboard (menu bar) is attached to every wizard-free
message so the main menu never disappears.
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
YEARS = ("2024", "2025", "2026")

MENU_TEXT = (
    "🎓 hsc-result-bot\n\n"
    "Everything is buttons — tap one:\n"
    "➕ Add student · 📋 List students · 📑 Results\n"
    "▶️ Run now · 🧭 Status · 🛑 Stop\n\n"
    "Power users: /add name board year roll reg · "
    "/del N · /top N · /result")

MENU_REPLY = {"keyboard": [
    ["➕ Add student", "📋 List students"],
    ["📑 Results", "▶️ Run now"],
    ["🧭 Status", "🛑 Stop"],
], "resize_keyboard": True, "is_persistent": True,
   "input_field_placeholder": "menu…"}

_BTN_ALIAS = {
    "➕ add student": "/add",
    "📋 list students": "/list",
    "📑 results": "/result",
    "▶️ run now": "/run",
    "🧭 status": "/status",
    "🛑 stop": "/stop",
}


def _std_text(raw: str) -> str:
    """Tolerate leading /command@botname tails."""
    return raw.split("@", 1)[0].strip()


def _kb(rows, placeholder=None, one_time=False):
    kb = {"keyboard": rows, "resize_keyboard": True}
    if placeholder:
        kb["input_field_placeholder"] = placeholder
    if one_time:
        kb["one_time_keyboard"] = True
    return kb


def _board_kb():
    rows, cur = [], []
    for b in BOARDS:
        cur.append(b.title())
        if len(cur) == 3:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append(["❌ Cancel"])
    return _kb(rows, "pick the board")


def _exam_kb():
    return _kb([[e.upper() for e in EXAMS], ["❌ Cancel"]], "pick the exam")


def _year_kb():
    return _kb([[y for y in YEARS], ["❌ Cancel"]], "pick the year")


def _confirm_kb():
    return _kb([["✅ Save student"], ["✏️ Start over"], ["❌ Cancel"]],
               "save this student?")


class Cockpit(threading.Thread):
    """Runs until shutdown_event; never leaks an exception to the brain."""

    def __init__(self, token: str, chat_id: str, store,
                 announce: bool = True, run_grind=None):
        super().__init__(daemon=True, name="cockpit")
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.store = store
        self.announce = announce
        self.run_grind = run_grind
        self.run_once = threading.Event()
        self.hard_stop = threading.Event()
        self.shutdown_event = threading.Event()
        self._offset = 0
        self._offset_ts = 0.0
        self._s = requests.Session()
        # wizard state: None | {"data": {...}, "step": <name>}
        self._wiz = None
        self._steps = ("name", "board", "exam", "year", "roll", "reg")

    # -- transport ---------------------------------------------------------
    def _api(self, method: str, **body):
        """Parsed Telegram response dict, or None on failure."""
        try:
            r = self._s.post(API.format(token=self.token, method=method),
                             json=body, timeout=35)
            if not r.ok:
                return None
            return r.json()
        except Exception:
            return None

    def send(self, text: str, kb=None) -> None:
        """Every free message rides the persistent menu bar."""
        body = {"chat_id": self.chat_id, "text": text,
                "disable_web_page_preview": True,
                "reply_markup": kb or MENU_REPLY}
        if not self._api("sendMessage", **body):
            body.pop("reply_markup", None)
            body["text"] = text.replace("*", "").replace("`", "")
            self._api("sendMessage", **body)

    def send_r(self, text: str, kb=None):
        """send but returns the message id (for inline-editable lists)."""
        body = {"chat_id": self.chat_id, "text": text,
                "disable_web_page_preview": True, "reply_markup": kb}
        r = self._api("sendMessage", **body)
        if r and r.get("ok"):
            return r["result"]["message_id"]
        return None

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
                if r.status_code == 409:   # previous poller still lingering
                    time.sleep(3)
                    continue
                r.raise_for_status()
                updates = (r.json() or {}).get("result") or []
            except Exception:
                time.sleep(5)
                continue
            if updates:
                self._offset = int(updates[-1]["update_id"]) + 1
                if time.time() - self._offset_ts > 121:
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
            self._on_callback(cb)
            return
        msg = update.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id") or "")
        if chat != self.chat_id:
            return  # strangers stay ignored
        text = (msg.get("text") or "").strip()
        if not text:
            return
        alias = _BTN_ALIAS.get(text.lower())
        if alias:
            self._command(_std_text(alias), msg.get("from") or {})
            return
        if self._wiz:
            self._wiz_input(text)
            return
        if not text.startswith("/"):
            # free text with no wizard: nudge back to buttons
            self.send("tap a button above, or /menu 🙏")
            return
        self._command(_std_text(text), msg.get("from") or {})

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
             self._c_cancel if head == "/cancel" else
             self._c_unknown)(argline)
        except Exception as e:
            self.send("⚠️ %s — %s" % (head, e or type(e).__name__))

    # -- inline priority editor -------------------------------------------
    def _list_view(self) -> dict:
        """text + inline keyboard for the live priority editor."""
        students = self.store.students()
        if not students:
            return {"text": "queue empty — tap ➕ Add student",
                    "kb": {"inline_keyboard": []}}
        lines = ["📋 queue — tap to reorder/remove"]
        ik = []
        for i, r in enumerate(students, 1):
            if r.get("delivered_at"):
                mark = "✅"
            elif r.get("found_at"):
                mark = "📨"
            else:
                mark = "⏳"
            gpa = (r.get("result") or {}).get("gpa")
            gpa_tail = " · GPA %s" % gpa if gpa else ""
            lines.append("%d. %s — %s %s roll %s %s%s"
                         % (i, r.get("name", "?"), r.get("board", "?"),
                            r.get("year", "?"), r.get("roll", "?"),
                            mark, gpa_tail))
            ik.append([
                {"text": "⬆️", "callback_data": "q:up:%d" % i},
                {"text": "⬇️", "callback_data": "q:down:%d" % i},
                {"text": "⭐ first", "callback_data": "q:top:%d" % i},
                {"text": "🗑", "callback_data": "q:del:%d" % i},
            ])
        return {"text": "\n".join(lines), "kb": {"inline_keyboard": ik}}

    def _push_list(self, msg_id=None) -> None:
        view = self._list_view()
        self._last_list = view["text"]
        if msg_id:
            r = self._api("editMessageText", chat_id=self.chat_id,
                          message_id=msg_id, text=view["text"],
                          reply_markup=view["kb"])
            if r and r.get("ok"):
                return
            # message unmodified / too old → fall through to a fresh send
        self.send_r(view["text"], view["kb"])

    def _on_callback(self, cb: dict) -> None:
        data = str(cb.get("data") or "")
        cb_id = cb.get("id") or ""
        msg = cb.get("message") or {}
        msg_id = msg.get("message_id")
        chat = str((msg.get("chat") or {}).get("id") or "")
        if chat != self.chat_id:
            self.answer(cb_id)
            return
        if not data.startswith("q:"):
            self.answer(cb_id)
            return
        try:
            _, op, n_s = data.split(":", 2)
            n = int(n_s)
        except ValueError:
            self.answer(cb_id)
            return

        def edit(st):
            if not (1 <= n <= len(st)):
                raise ValueError("row %d gone — resend the list" % n)
            if op == "del":
                gone = st.pop(n - 1)
                self._cb_note = "removed %s" % gone.get("name", "?")
                return st
            j = {"up": n - 2, "down": n, "top": 0}[op]
            j = max(0, min(len(st) - 1, j))
            row = st.pop(n - 1)
            st.insert(j, row)
            verbs = {"up": "moved up", "down": "moved down",
                     "top": "now polling first"}
            self._cb_note = "%s — %s" % (row.get("name", "?"),
                                         verbs.get(op, "moved"))
            return st

        try:
            self.store.mutate_students(edit)
        except Exception as e:
            self.answer(cb_id, str(e) or "failed")
            self._push_list()
            return
        view = self._list_view()
        self.answer(cb_id, self._cb_note)
        if msg_id and view["text"] == getattr(self, "_last_list", None):
            return  # nothing moved — don't reflood the same list
        self._push_list(msg_id)

    # -- the add-wizard ----------------------------------------------------
    def _c_add(self, args: str) -> None:
        # power path: the full one-liner still works
        if args.strip():
            self._add_row(args)
            return
        self._wiz = {"data": {}, "step": "name"}
        self.send("🆕 add a student, step by step.\n\n"
                  "1/6 — what's their name?\n"
                  "(type /cancel anytime to bail)", _kb(
                      [["❌ Cancel"]], "type the name"))

    def _wiz_input(self, text: str) -> None:
        if not self._wiz:
            return
        t = text.strip()
        if t.lower() in ("cancel", "❌ cancel", "/cancel"):
            self._wiz = None
            self.send("❌ cancelled — nothing saved")
            return
        if self._wiz["step"] == "confirm_button":
            self._wiz_confirm(t)
            return
        data, step = self._wiz["data"], self._wiz["step"]
        t = text.strip()
        note = ""
        if step == "name":
            if len(t) > 24:
                self.send("keep the name under 24 chars — try again")
                return
            data["name"] = t
            nxt = "board"
            self.send("2/6 — which board?", _board_kb())
        elif step == "board":
            b = t.lower()
            if b not in BOARDS:
                self.send("that's not on the board list — tap one",
                          _board_kb())
                return
            data["board"] = b
            nxt = "exam"
            self.send("3/6 — which exam?", _exam_kb())
        elif step == "exam":
            e = t.lower()
            if e not in EXAMS:
                self.send("tap one of the exam buttons", _exam_kb())
                return
            data["exam"] = e
            nxt = "year"
            self.send("4/6 — which year?", _year_kb())
        elif step == "year":
            if t not in YEARS:
                self.send("tap one of the year buttons", _year_kb())
                return
            data["year"] = t
            nxt = "roll"
            self.send("5/6 — roll number? (digits only; type /cancel to bail)",
                      _kb([["❌ Cancel"]], "type the roll"))
        elif step == "roll":
            if not t.isdigit() or not (4 <= len(t) <= 10):
                self.send("roll needs 4–10 digits — try again",
                          _kb([["❌ Cancel"]], "type the roll"))
                return
            data["roll"] = t
            nxt = "reg"
            self.send("6/6 — registration number? (digits)",
                      _kb([["❌ Cancel"]], "type the reg"))
        elif step == "reg":
            if not t.isdigit() or not (6 <= len(t) <= 15):
                self.send("reg needs 6–15 digits — try again",
                          _kb([["❌ Cancel"]], "type the reg"))
                return
            data["reg"] = t
            nxt = "confirm"
            self.send(
                "🧾 check it:\n\n"
                "name  %s\nboard  %s\nexam  %s\nyear  %s\n"
                "roll  %s\nreg  %s" % (data["name"], data["board"],
                                       data["exam"], data["year"],
                                       data["roll"], data["reg"]),
                _confirm_kb())
        else:
            return
        if nxt == "confirm":
            self._wiz["step"] = "confirm_button"
        else:
            self._wiz["step"] = nxt

    def _wiz_confirm(self, t: str) -> None:
        slot = t.lower()
        if slot.startswith("✅") or slot == "save student":
            d = self._wiz["data"]

            def append(st):
                if any(str(x.get("roll")) == d["roll"] for x in st):
                    raise ValueError(
                        "roll %s is already in the queue" % d["roll"])
                return st + [{
                    "name": d["name"], "board": d["board"], "year": d["year"],
                    "exam": d["exam"], "roll": d["roll"], "reg": d["reg"],
                    "added_at": datetime.datetime.now().isoformat(
                        timespec="seconds"),
                    "found_at": "", "found_via": "",
                }]

            try:
                students = self.store.mutate_students(append)
            except ValueError as e:
                self.send("⚠️ %s\n\nfix it with ✏️ Start over, or ❌ Cancel"
                          % e, _confirm_kb())
                return
            self._wiz = None
            self.send("✅ %s saved — %d in the queue. Result day fires it, "
                      "or tap ▶️ Run now to try immediately." %
                      (d["name"], len(students)))
            return
        if slot.startswith("✏") or "start over" in slot:
            self._wiz = {"data": {}, "step": "name"}
            self.send("♻️ starting over.\n\n1/6 — what's their name?",
                      _kb([["❌ Cancel"]], "type the name"))
            return
        self.send("tap ✅ Save or ✏️ Start over", _confirm_kb())

    def _add_row(self, argline: str) -> None:
        parts = [p for p in argline.split() if p]
        if len(parts) not in (5, 6):
            raise ValueError(
                "usage: /add name board year roll reg [exam]\n"
                "boards: %s — or just tap ➕ Add student"
                % ", ".join(BOARDS))
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

        def append(st):
            if any(str(x.get("roll")) == roll for x in st):
                raise ValueError("roll %s already in queue" % roll)
            return st + [{
                "name": name, "board": board.lower(), "year": year,
                "exam": exam, "roll": roll, "reg": reg,
                "added_at": datetime.datetime.now().isoformat(
                    timespec="seconds"),
                "found_at": "", "found_via": "",
            }]

        students = self.store.mutate_students(append)
        self.send("➕ %s added at %d of %d" % (name, len(students),
                                              len(students)))

    # -- commands ----------------------------------------------------------
    def _c_unknown(self, _) -> None:
        self.send("that one's not on the menu — /help")

    def _c_start(self, _) -> None:
        self.send(MENU_TEXT)

    _c_help = _c_start
    _c_menu = _c_start

    def _c_cancel(self, _) -> None:
        if self._wiz:
            self._wiz = None
            self.send("❌ cancelled — nothing saved")
        else:
            self.send("nothing to cancel")

    def _c_list(self, _) -> None:
        self._push_list()

    def _c_del(self, args: str) -> None:
        parts = args.split()
        if not parts or not parts[0].isdigit():
            self._push_list()
            self.send("🗑 tap 🗑 on the list, or /del 2")
            return
        n = int(parts[0])

        def drop(st):
            if not (1 <= n <= len(st)):
                raise ValueError("no row %d — queue holds %d"
                                 % (n, max(1, len(st))))
            gone = st.pop(n - 1)
            self._del_note = gone.get("name", "?")
            return st

        students = self.store.mutate_students(drop)
        self.send("🗑 removed %s — queue now %d row(s)"
                  % (self._del_note, len(students)))

    def _c_top(self, args: str) -> None:
        parts = args.split()
        if not parts or not parts[0].isdigit():
            self._push_list()
            self.send("⭐ tap ⭐ first on the list, or /top 2")
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
            row["found_at"] = ""   # re-promote re-enters the grind
            row["found_via"] = ""
            row["delivered_at"] = ""
            self._top_note = row.get("name", "?")
            return [row] + st

        students = self.store.mutate_students(move)
        self.send("⭐ %s polls first now (%d row(s))"
                  % (self._top_note, len(students)))

    def _c_result(self, _) -> None:
        rows = self.store.students(fresh=True)
        landed = [r for r in rows if r.get("result")]
        if not landed:
            self.send("no stored results yet — tap ▶️ Run now once")
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
            self.send("⏳ already polling — verdict landing in a moment…")
            return
        self.run_once.set()
        self.send("🔬 polling one pass right now — verdict in a minute…")

    def _c_stop(self, _) -> None:
        today = datetime.datetime.now().isoformat()[:10]
        self.store.update_state(polling=False, stop_date=today)
        self.hard_stop.set()
        self.send("🛑 window closed — polling stops; tomorrow's cron "
                  "re-opens it (or ▶️ Run now pushes one pass anyway)")

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
            "last cycle %s · next in %ss\n"
            "wake %s Asia/Dhaka"
            % (status.get("mode", "?"), st.get("polling", True),
               status.get("students", "?"), status.get("found", "?"),
               mirror_line, ago, status.get("interval", 30),
               status.get("wake", "?")))
