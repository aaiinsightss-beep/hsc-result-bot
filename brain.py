# -*- coding: utf-8 -*-
"""Cloud brain — GitHub Actions entrypoint.

Boot modes (BRAIN_MODE):
  wake (cron 10:55 Dhaka) — sleep in-runner to the exact 10:59:55 second,
                            then grind the queue every 30s cycle.
  now  (dispatch / button) — poll ONE pass right now, reply the verdict, exit.

Grind: queue top-down × enabled mirrors staggered ~5s; every found card is
delivered to the SAME chat; the rest of the queue keeps grinding down.

Exception classifier (self-healing):
  403 tar-pit      → cool-off 90s + brand-new session
  419              → full session rebuild
  network dead     → 10s cool + next mirror
  repeated fail    → 60s backoff
  every mirror down→ telegram one-liner (rate-limited 30 min)
  captcha miss     → mirrors.py already regrabs x4 in-loop

State flushes to the Gist after every event. /stop closes the window; the
next daily cron re-opens polling. Deadline reached with rows outstanding →
exit 3 so the workflow re-runs once.
"""
import datetime
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import mirrors  # noqa: E402
import notifier  # noqa: E402
from cockpit import Cockpit  # noqa: E402
from queue_store import QueueStore  # noqa: E402

NOT_PUBLISHED = mirrors.NotPublished
PARSE_ERROR = mirrors.ParseError
NET_ERR = (requests.ConnectionError, requests.Timeout)

WAKE_HOUR, WAKE_MIN, WAKE_SEC = 10, 59, 55
CYCLE_INTERVAL = 30          # seconds between full-queue cycles
MIRROR_STAGGER = 5           # second/third mirror fired this much later
REPEAT_FAIL_BACKOFF = 60     # repeated generic failures on one mirror
TARPIT_COOL = 90             # 403/419 — cool-off + fresh session
NET_COOL = 10                # dead pipe — short cool, try next mirror
BLAME_RATE = 30 * 60         # telegram blame once per 30 min, max


def _say(token: str, chat: str, text: str) -> None:
    try:
        requests.post(
            notifier.API.format(token=token, method="sendMessage"),
            json={"chat_id": chat, "text": text}, timeout=15,
        )
    except Exception:
        pass


def _now_bdt() -> datetime.datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Dhaka"))
    except Exception:
        return datetime.datetime.now().astimezone()


def _mirror_cfgs() -> dict:
    """mirrors.json first (repo shape), config.json mirrors block second."""
    p = BASE / "mirrors.json"
    if p.exists():
        return json.loads(p.read_text("utf-8-sig"))
    p = BASE / "config.json"
    if p.exists():
        return json.loads(p.read_text("utf-8-sig")).get("mirrors", {})
    raise RuntimeError("no mirrors.json / config.json next to brain.py")


def _class_exc(e: Exception) -> tuple:
    """-> (kind, cool_seconds, rebuild_session)"""
    code = getattr(getattr(e, "response", None), "status_code", None)
    if code == 403:
        return ("403", TARPIT_COOL, True)
    if code == 419:
        return ("419", TARPIT_COOL, True)
    if isinstance(e, NET_ERR):
        return ("net", NET_COOL, False)
    if isinstance(e, PARSE_ERROR):
        return ("mirror", REPEAT_FAIL_BACKOFF, True)
    return ("raw", REPEAT_FAIL_BACKOFF, True)


class MirrorHealth:
    """Cooldowns + failure kinds per mirror, cyclical."""

    def __init__(self, names):
        self.hc = {n: {"cool_until": 0.0, "fails": 0, "kind": "",
                       "rebuild": False} for n in names}

    def cooling(self, name: str) -> bool:
        return time.time() < self.hc[name]["cool_until"]

    def note(self, name: str, kind: str, cool: float, rebuild: bool) -> None:
        h = self.hc[name]
        if kind == "hit":
            h["kind"] = "hit"
            h["cool_until"] = 0.0
            h["fails"] = 0
            h["rebuild"] = False
            return
        h["kind"] = kind
        h["cool_until"] = time.time() + cool
        h["fails"] += 1
        h["rebuild"] = h["rebuild"] or rebuild

    def consume_rebuild(self) -> bool:
        """Any mirror demanded a fresh session — one rebuild covers all."""
        want = any(h["rebuild"] for h in self.hc.values())
        for h in self.hc.values():
            h["rebuild"] = False
        return want


def _flush_status(store: QueueStore, health: MirrorHealth, mode: str,
                  students: list) -> None:
    found = sum(1 for r in students if r.get("found_at"))
    mirrors_state = {}
    for n, h in health.hc.items():
        mirrors_state[n] = h["kind"] or "idle"
    store.update_state(status={
        "mode": mode,
        "students": len(students),
        "found": found,
        "last_cycle_ts": time.time(),
        "interval": CYCLE_INTERVAL,
        "wake": "%02d:%02d:%02d" % (WAKE_HOUR, WAKE_MIN, WAKE_SEC),
        "mirrors": mirrors_state,
    })


def _fmt_verdict(students: list, health: MirrorHealth) -> str:
    out = ["*🔬 one-pass verdict*"]
    any_live = False
    for i, row in enumerate(students, 1):
        if row.get("found_at"):
            out.append("%d. %s — ✅ delivered via %s"
                       % (i, row.get("name", "?"), row["found_via"]))
            continue
        any_live = True
        marks = []
        for n, h in health.hc.items():
            if h["kind"] in ("hit", "not_published", "idle"):
                marks.append("%s ⏳ not out" % n.split(".")[0])
            else:
                marks.append("%s ⚠ %s" % (n.split(".")[0], h["kind"]))
        out.append("%d. %s — ❌ not out (%s)"
                   % (i, row.get("name", "?"), " · ".join(marks)))
    if not any_live:
        out.append("all rows already delivered")
    return "\n".join(out)


def _sleep_until_wake(store: QueueStore) -> None:
    """Schedule boot: hold inside the runner to the exact 10:59:55 BDT."""
    now = _now_bdt()
    tgt = now.replace(hour=WAKE_HOUR, minute=WAKE_MIN, second=WAKE_SEC,
                      microsecond=0)
    if tgt <= now:
        return  # cron drifted late — window already open, just grind
    while True:
        now = _now_bdt()
        remain = (tgt - now).total_seconds()
        if remain <= 0:
            return
        time.sleep(min(5.0, remain))


def _one_pass(token, chat, adps, store, health, session, deliver=True):
    """One mirror-staggered sweep of the whole queue. Returns (session_keep,
    found_now)."""
    students = store.students()
    if not students:
        return 0
    found_now = 0
    for row in students:
        if row.get("found_at"):
            continue
        for mi, adp in enumerate(adps):
            if health.cooling(adp.name):
                continue
            if mi:
                time.sleep(MIRROR_STAGGER)
            scfg = {"student": {
                "examination": row.get("exam") or "ssc",
                "board": row.get("board") or "dhaka",
                "year": row.get("year") or "2026",
                "roll": str(row.get("roll") or ""),
                "reg": str(row.get("reg") or ""),
            }}
            try:
                res = mirrors.pull(adp, scfg, session=session)
            except NOT_PUBLISHED:
                health.note(adp.name, "not_published", 0, False)
                continue
            except NET_ERR as e:
                kind, cool, rebuild = _class_exc(e)
                health.note(adp.name, kind, cool, rebuild)
                continue
            except Exception as e:
                kind, cool, rebuild = _class_exc(e)
                health.note(adp.name, kind, cool, rebuild)
                continue
            health.note(adp.name, "hit", 0, False)
            row["found_at"] = _now_bdt().isoformat(timespec="seconds")
            row["found_via"] = adp.name
            if deliver:
                try:
                    notifier.send_result(
                        {"telegram": {"bot_token": token, "chat_id": chat}}, res)
                except Exception as e:
                    _say(token, chat,
                         "⚠️ %s found but the card failed to send (%s) — "
                         "retrying next cycle"
                         % (row.get("name", "?"), type(e).__name__))
                    row["found_at"] = ""
                    row["found_via"] = ""
            found_now += 1
            store.save_students(students)
            break  # row served — next student
    return found_now


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    gist_id = os.environ.get("GIST_ID", "").strip()
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                           "GIST_TOKEN", "GIST_ID")
               if not os.environ.get(k, "").strip()]
    if missing:
        raise RuntimeError("secrets missing: %s" % ", ".join(missing))
    mode = os.environ.get("BRAIN_MODE", "wake").strip() or "wake"

    store = QueueStore(gist_token, gist_id)
    st = store.state()
    today = _now_bdt().date().isoformat()
    if st.get("polling") is False:
        if mode == "wake" and (st.get("stop_date") or "") == today:
            _say(token, chat, "🛑 /stop'd earlier today — window stays closed")
            return 0
        store.update_state(polling=True, stop_date="")
        _say(token, chat, "🔓 new day at the cron — polling re-opened")

    adps = []
    for key, m in _mirror_cfgs().items():
        if not m.get("enabled"):
            continue
        try:
            cfg = dict(m)
            cfg.setdefault("name", key)
            adps.append(mirrors.build(cfg, 20))
        except Exception as e:
            _say(token, chat, "🧱 adapter %s broke at boot (%s: %s)"
                 % (key, type(e).__name__, e))
    if not adps:
        _say(token, chat, "☠ no mirror adapters — check mirrors.json types")
        return 1
    names = [a.name for a in adps]

    if mode == "wake":
        _sleep_until_wake(store)
        _say(token, chat, "⏰ window open %s — grinding %d row(s)"
             % (_now_bdt().isoformat()[11:19], len(store.students())))

    cockpit = Cockpit(token, chat, store)
    cockpit.start()

    health = MirrorHealth(names)
    timeout_min = int(os.environ.get("BRAIN_MAX_MINUTES", "300"))
    deadline = time.time() + timeout_min * 60
    last_blame = 0.0
    session = mirrors._fresh()

    try:
        while time.time() < deadline:
            if cockpit.hard_stop.is_set():
                store.update_state(polling=False,
                                   stop_date=_now_bdt().date().isoformat())
                _say(token, chat, "🛑 stopped — %d row(s) outstanding"
                     % len([r for r in store.students() if not r.get("found_at")]))
                return 0
            q = store.students()
            pending = [r for r in q if not r.get("found_at")]
            if not pending:
                _say(token, chat, "🎓 queue drained — every row found & delivered")
                return 0
            if not any(not health.cooling(n) for n in names):
                if time.time() - last_blame > BLAME_RATE:
                    last_blame = time.time()
                    _say(token, chat,
                         "🥴 every mirror cooling (%s) — the board's dead, "
                         "not you; retrying in %ds" % (
                             ", ".join("%s: %s" % (n.split(".")[0], h["kind"] or "?")
                                       for n, h in health.hc.items()),
                             CYCLE_INTERVAL))
                time.sleep(CYCLE_INTERVAL)
                continue

            if health.consume_rebuild():
                session = mirrors._fresh()  # 403/419 — brand-new session
            _one_pass(token, chat, adps, store, health, session, deliver=True)
            q = store.students()
            _flush_status(store, health, mode, q)
            if mode == "now":
                _say(token, chat, _fmt_verdict(q, health))
                return 0
            if not [r for r in q if not r.get("found_at")]:
                _say(token, chat, "🎓 queue drained — every row found & delivered")
                return 0

            wait = CYCLE_INTERVAL
            while wait > 0 and not cockpit.run_once.is_set() \
                    and not cockpit.hard_stop.is_set() \
                    and time.time() < deadline:
                time.sleep(min(1.0, wait))
                wait -= 1
    except KeyboardInterrupt:
        return 0
    _outstanding = len([r for r in store.students() if not r.get("found_at")])
    _say(token, chat,
         "⏱ window closed after %d min — %d row(s) still pending; "
         "workflow re-runs once" % (timeout_min, _outstanding))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
