# -*- coding: utf-8 -*-
"""Cloud brain — GitHub Actions entrypoint. One file, four modes.

BRAIN_MODE:
  wake   — the classic window: sleep in-runner to the exact 10:59:55 BDT,
           grind the queue every 30s cycle until everyone's found, a
           /stop, or the deadline. deadliners exit 3 → workflow re-runs once.
  now    — poll ONE pass right now, telegram the verdict, exit.
  sweep  — watchdog: if the chain's heartbeat is fresh, exit silently;
           else re-seed a chain link, then drain Telegram commands for
           ~45s (bridge while the chain spins up).
  chain  — 24/7 worker link: cockpit answers Telegram 24/7, heartbeats
           state.svc_ts every 45s, and grinds whenever pending × polling
           × wake-window (night-throttled to 300s cycles). The workflow
           dispatches the next link at segment end — one always-on cloud
           service, $0 on a public repo.

Exception classifier (self-healing):
  403 → cool 90s + fresh session · 419 → rebuild · net dead → 10s +
  next mirror · repeat fail → 60s · mirrors all down → telegram blame
  (1/30min) · captcha miss → mirrors.py regrabs x4 in-loop.
"""
import datetime
import json
import os
import sys
import threading
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
CYCLE_INTERVAL = 30          # seconds between full-queue cycles (day)
NIGHT_INTERVAL = 300         # off-hours throttle — don't hammer the board
MIRROR_STAGGER = 5           # second/third mirror fired this much later
REPEAT_FAIL_BACKOFF = 60     # repeated generic failures on one mirror
TARPIT_COOL = 90             # 403/419 — cool-off + fresh session
NET_COOL = 10                # dead pipe — short cool, try next mirror
BLAME_RATE = 30 * 60         # telegram blame once per 30 min, max
SCHED = {"date": "", "hour": WAKE_HOUR, "minute": WAKE_MIN, "second": WAKE_SEC}

CHAIN_BEAT = 510             # heartbeat cadence — ~2 PATCH/link, budget-safe
CHAIN_FRESH = 11 * 60        # watchdog: heartbeat older than this = reseed
STANDDOWN_FRESH = 9 * 60     # svc_ts younger than this = someone owns the wheel


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


def _build_adapters(token: str, chat: str) -> list:
    out = []
    for key, m in _mirror_cfgs().items():
        if not m.get("enabled"):
            continue
        try:
            cfg = dict(m)
            cfg.setdefault("name", key)
            out.append(mirrors.build(cfg, 20))
        except Exception as e:
            _say(token, chat, "🧱 adapter %s broke at boot (%s: %s)"
                 % (key, type(e).__name__, e))
    return out


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
        want = any(h["rebuild"] for h in self.hc.values())
        for h in self.hc.values():
            h["rebuild"] = False
        return want


def _sleep_tick(total: float, break_fn=None) -> None:
    end = time.time() + total
    while time.time() < end:
        if break_fn and break_fn():
            return
        time.sleep(1.0)


def _fetch_sched(store) -> dict:
    """Result-day schedule shared by every worker via the gist state —
    written once per boot/config-save by serve. Cloud brains read it."""
    try:
        sc = store.state().get("schedule") or {}
        return {
            "date": str(sc.get("date") or ""),
            "hour": int(sc.get("hour", WAKE_HOUR)),
            "minute": int(sc.get("minute", WAKE_MIN)),
            "second": int(sc.get("second", WAKE_SEC)),
        }
    except Exception:
        return {"date": "", "hour": WAKE_HOUR, "minute": WAKE_MIN,
                "second": WAKE_SEC}


def _sched_set(store) -> dict:
    global _SCHED
    _SCHED = _fetch_sched(store)
    return _SCHED


def _wake_open(t_now: datetime.datetime, sched: dict = None) -> bool:
    """Grind gate: today's wake second has passed AND the result day has
    arrived (a future target date means nothing fires until then)."""
    sched = sched or _SCHED
    if sched and sched.get("date"):
        try:
            day = datetime.date.fromisoformat(sched["date"])
            if t_now.date() < day:
                return False
        except ValueError:
            pass
    hour = (sched or {}).get("hour", WAKE_HOUR)
    minute = (sched or {}).get("minute", WAKE_MIN)
    second = (sched or {}).get("second", WAKE_SEC)
    if t_now.hour > hour:
        return True
    return t_now.hour == hour and (
        t_now.minute * 60 + t_now.second) >= (minute * 60 + second)


class _NullCockpit:
    """Stand-in cockpit when another worker has the wheel: all loops
    still run, zero Telegram long-polls (same-token getUpdates would
    409-conflict against the live worker's)."""
    def __init__(self):
        self.run_once = threading.Event()
        self.hard_stop = threading.Event()

    def start(self):
        pass

    def stop(self):
        pass


_LAST_STATUS = {"key": None, "ts": 0.0}


def _flush_status(store: QueueStore, health: MirrorHealth, mode: str,
                  students: list, force: bool = False) -> None:
    """Status in the gist — throttled to ~one PATCH/5min unless the shape
    of the queue changed (shorts the hourly rate budget hard)."""
    found = sum(1 for r in students if r.get("found_at"))
    key = ("%s|%d|%d" % (mode, len(students), found),
           tuple(sorted((n, h["kind"]) for n, h in health.hc.items())))
    recent = (time.time() - _LAST_STATUS["ts"]) < 900
    same = key == _LAST_STATUS["key"]
    if not force and recent and same:
        return
    _LAST_STATUS["key"] = key
    _LAST_STATUS["ts"] = time.time()
    mirrors_state = {n: (h["kind"] or "idle") for n, h in health.hc.items()}
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
    any_out = False
    for i, row in enumerate(students, 1):
        if row.get("found_at") and row.get("delivered_at"):
            out.append("%d. %s — ✅ delivered via %s"
                       % (i, row.get("name", "?"), row.get("found_via") or "?"))
            continue
        if row.get("found_at"):
            any_out = True
            out.append("%d. %s — 📨 result found, card re-sending…"
                       % (i, row.get("name", "?")))
            continue
        any_out = True
        marks = []
        for n, h in health.hc.items():
            if h["kind"] in ("hit", "not_published", "idle"):
                marks.append("%s ⏳ not out" % n.split(".")[0])
            else:
                marks.append("%s ⚠ %s" % (n.split(".")[0], h["kind"]))
        out.append("%d. %s — ❌ not out (%s)"
                   % (i, row.get("name", "?"), " · ".join(marks)))
    if not any_out:
        out.append("all rows already delivered")
    return "\n".join(out)


def _owed(row: dict) -> bool:
    """A row still owes work: not found yet, or found but its card never
    actually landed in Telegram (delivered_at = the real receipt)."""
    return not row.get("found_at") or not row.get("delivered_at")


def _same_row(x: dict, row: dict) -> bool:
    return (str(x.get("roll")) == str(row.get("roll"))
            and str(x.get("reg")) == str(row.get("reg")))


def _stamp_row(store, base_row: dict, patch: dict):
    """One locked pass: merge patch (matched by roll+reg) onto the
    CURRENT gist rows and write — never a stale whole-list write.
    Returns (served_elsewhere, row_as_now_in_the_gist)."""
    served = {"flag": False}

    def fn(st):
        rows, hit = [], False
        for x in st:
            if not _same_row(x, base_row):
                rows.append(x)
                continue
            hit = True
            if x.get("delivered_at"):
                served["flag"] = True
            fresh = dict(x)
            fresh.update(patch)
            rows.append(fresh)
        if not hit:
            rows.append(dict(base_row, **patch))
        return rows

    students = store.mutate_students(fn)
    fresh_row = next((x for x in students
                      if _same_row(x, base_row)), dict(base_row, **patch))
    return served["flag"], fresh_row


def _deliver_card(token, chat, row, store) -> None:
    """Send the stored result card and stamp the receipt. Failures keep
    the row owed — the next pass retries automatically."""
    scfg = {"telegram": {"bot_token": token, "chat_id": chat},
            "student": {"examination": row.get("exam") or "ssc"}}
    try:
        notifier.send_result(scfg, row.get("result") or {})
        _stamp_row(store, row, {
            "delivered_at": _now_bdt().isoformat(timespec="seconds")})
    except Exception as e:
        print("[brain] %s card retry dead: %r — stays owed"
              % (row.get("roll"), e), flush=True)


def _one_pass(token, chat, adps, store, health, session, deliver=True):
    """One mirror-staggered sweep of the queue. Returns found count."""
    students = store.students()
    if not students:
        return 0
    found_now = 0
    for row in students:
        if row.get("found_at") and row.get("delivered_at"):
            continue
        if row.get("found_at") and row.get("result"):
            # found by an earlier worker but the card never landed —
            # deliver from the stored payload, no board poll needed
            _deliver_card(token, chat, row, store)
            continue
        for mi, adp in enumerate(adps):
            if health.cooling(adp.name):
                continue
            if mi:
                time.sleep(MIRROR_STAGGER)
            scfg = {"telegram": {"bot_token": token, "chat_id": chat},
                    "student": {
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
                print("[brain] %-32s %s: not published yet"
                      % (adp.name, row.get("name")), flush=True)
                continue
            except NET_ERR as e:
                kind, cool, rebuild = _class_exc(e)
                health.note(adp.name, kind, cool, rebuild)
                print("[brain] %-32s %s: %s — cool %ds"
                      % (adp.name, row.get("name"), kind, cool), flush=True)
                continue
            except Exception as e:
                kind, cool, rebuild = _class_exc(e)
                health.note(adp.name, kind, cool, rebuild)
                print("[brain] %-32s %s: %s (%s) — cool %ds"
                      % (adp.name, row.get("name"), kind, e, cool), flush=True)
                continue
            health.note(adp.name, "hit", 0, False)
            print("[brain] %-32s %s: HIT — card in flight"
                  % (adp.name, row.get("name")), flush=True)
            patch = {"found_at": _now_bdt().isoformat(timespec="seconds"),
                     "found_via": adp.name,
                     "result": res}
            served_already, fresh_row = _stamp_row(store, row, patch)
            if served_already:
                # a sibling delivered this exact row while we pulled
                print("[brain] %-32s already served by another worker"
                      % adp.name, flush=True)
                break
            row = fresh_row
            if deliver:
                try:
                    notifier.send_result(scfg, res)
                    _stamp_row(store, row, {
                        "delivered_at":
                            _now_bdt().isoformat(timespec="seconds")})
                except Exception as e:
                    _say(token, chat,
                         "⚠️ %s found but the card failed to send (%s) — "
                         "retrying next cycle"
                         % (row.get("name", "?"), type(e).__name__))
            found_now += 1
            break  # row served — next student
    return found_now


def _make_dispatch(mode: str):
    """Cockpit-in-Actions: fire a grind/chain run through the Actions API.
    Needs GITHUB_TOKEN with actions:write — outside Actions it degrades
    gracefully."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    tok = os.environ.get("GITHUB_TOKEN", "")

    def fire() -> bool:
        if not (repo and tok):
            return False
        r = requests.post(
            "https://api.github.com/repos/%s/actions/workflows/brain.yml/dispatches"
            % repo,
            headers={"Authorization": "Bearer %s" % tok,
                     "Accept": "application/vnd.github+json"},
            json={"ref": "main", "inputs": {"mode": mode}},
            timeout=15,
        )
        return r.status_code == 204
    return fire


def _chain_fresh(store) -> bool:
    try:
        ts = float(store.state().get("svc_ts") or 0)
        return (time.time() - ts) < CHAIN_FRESH
    except Exception:
        return False


def _worker_alive(store) -> bool:
    """Any heartbeat fresher than STANDDOWN_FRESH means serve/another
    chain link owns the wheel — cloud links stand down so the queue
    never gets double-polled."""
    try:
        ts = float(store.state().get("svc_ts") or 0)
        return (time.time() - ts) < STANDDOWN_FRESH
    except Exception:
        return False


def _beat_svc(store, stop_evt: threading.Event) -> None:
    """svc_ts heartbeat — the watchdog's proof the 24/7 worker is alive.

    gist_update resource = 100 PATCHes/hour per user pool, so beats are
    wide apart: immediate claim at boot, then one every ~8.5 min. The
    watchdog re-seeds only past CHAIN_FRESH.
    """
    def beat():
        while not stop_evt.is_set():
            try:
                if not stop_evt.is_set():
                    store.update_state(svc_ts=time.time())
            except Exception:
                pass
            stop_evt.wait(CHAIN_BEAT)
    threading.Thread(target=beat, name="svc-beat", daemon=True).start()


def _sleep_until_wake(store) -> None:
    now = _now_bdt()
    tgt = now.replace(hour=WAKE_HOUR, minute=WAKE_MIN, second=WAKE_SEC,
                      microsecond=0)
    if tgt <= now:
        return
    while True:
        now = _now_bdt()
        remain = (tgt - now).total_seconds()
        if remain <= 0:
            return
        time.sleep(min(5.0, remain))


def _grind_loop(token, chat, store, cockpit, deadline=None):
    """The window engine, shared by wake/now/chain semantics.

    deadline=None — runs until drain, /stop, or forever (drain parks).
    deadline set — the same, plus it exits when the runner's clock says
    so (the chain link hands the wheel to its successor on time).
    """
    names = [a.name for a in _ADPS]
    health = _HEALTH
    mode = os.environ.get("BRAIN_MODE", "wake")
    if deadline is None:
        timeout_min = int(os.environ.get("BRAIN_MAX_MINUTES", "300"))
        deadline = time.time() + timeout_min * 60
    last_blame = 0.0
    session = mirrors._fresh()

    while time.time() < deadline:
        if cockpit.hard_stop.is_set():
            cockpit.hard_stop.clear()
            store.update_state(polling=False,
                               stop_date=_now_bdt().date().isoformat())
            _say(token, chat, "🛑 window closed — tomorrow's wake re-opens it")
            return
        if health.consume_rebuild():
            session = mirrors._fresh()
        q = store.students()
        pending = [r for r in q if _owed(r)]
        if not pending:
            _say(token, chat, "🎓 queue drained — every row found & delivered")
            return
        if not any(not health.cooling(n) for n in names):
            if time.time() - last_blame > BLAME_RATE:
                last_blame = time.time()
                _say(token, chat,
                     "🥴 every mirror cooling (%s) — the board's dead, "
                     "not you; retrying in %ds" % (
                         ", ".join("%s: %s" % (n.split(".")[0],
                                               h["kind"] or "?")
                                   for n, h in health.hc.items()),
                         CYCLE_INTERVAL))
            _sleep_tick(CYCLE_INTERVAL,
                        lambda: cockpit.run_once.is_set())
            continue
        _one_pass(token, chat, _ADPS, store, health, session, deliver=True)
        q = store.students()
        _flush_status(store, health, mode, q)
        if mode == "now" or not [r for r in q if _owed(r)]:
            _say(token, chat, _fmt_verdict(q, health))
            return
        ivl = CYCLE_INTERVAL
        now_h = _now_bdt().hour
        if now_h < 9 or now_h >= 23:
            ivl = NIGHT_INTERVAL
        _sleep_tick(ivl, lambda: cockpit.run_once.is_set()
                    or cockpit.hard_stop.is_set())
        cockpit.run_once.clear()


# populated at boot in main(); the two globals the grind loop reads
_ADPS = []
_HEALTH = None


def _dispatch_flag(v: str) -> None:
    """Tell the workflow's next-link step whether to keep the chain alive.

    Stand-down / drained links write CHAIN=0 so the yml step skips the
    gh dispatch — the */10 cron sweep handles any future re-seed. Only a
    link that actually ground (or idled with pending rows) hands off.
    """
    p = os.environ.get("GITHUB_ENV")
    if p:
        try:
            with open(p, "a", encoding="utf-8") as f:
                f.write("CHAIN=%s\n" % v)
        except OSError:
            pass


def _chain_session(token: str, chat: str, store: QueueStore) -> int:
    """24/7 link: cockpit answers everything; grind fires when the wake
    window is open and rows are pending. Lives ~11 min, exits, workflow
    dispatches the next link. Stands down instantly if a local serve
    (or another link) is already heartbeating svc_ts."""
    if _worker_alive(store):
        # silent — a text per link here is what spammed the chat
        print("[brain] chain: a worker has the wheel — silent stand-down",
              flush=True)
        _dispatch_flag("0")
        return 0
    _sched_set(store)
    stop_evt = threading.Event()
    _beat_svc(store, stop_evt)
    cockpit = Cockpit(token, chat, store, announce=False, run_grind=None)
    cockpit.start()

    live = int(os.environ.get("BRAIN_CHAIN_SECONDS", "660"))
    deadline = time.time() + live
    while time.time() < deadline:
        if cockpit.hard_stop.is_set():
            cockpit.hard_stop.clear()
            store.update_state(polling=False,
                               stop_date=_now_bdt().date().isoformat())
            _say(token, chat, "🛑 window closed — tomorrow's wake re-opens it")
        st = store.state()
        q = store.students()
        pending = [r for r in q if _owed(r)]
        t = _now_bdt()
        can = bool(st.get("polling", True)) and bool(pending) and _wake_open(t)
        if not can:
            _sleep_tick(min(60.0, max(1.0, deadline - time.time())),
                        lambda: cockpit.run_once.is_set()
                        or cockpit.hard_stop.is_set())
            if cockpit.run_once.is_set():
                cockpit.run_once.clear()
                cockpit.hard_stop.clear()
                # explicit /run — disregard hold state for one pass
                _say(token, chat, "🔬 one pass out-of-window as asked…")
                _one_pass(token, chat, _ADPS, store, _HEALTH, mirrors._fresh())
                _flush_status(store, _HEALTH, "chain", store.students())
                _say(token, chat, _fmt_verdict(store.students(), _HEALTH))
            continue
        _grind_loop(token, chat, store, cockpit, deadline=deadline)
        _flush_status(store, _HEALTH, "chain", store.students())
        if not [r for r in store.students() if _owed(r)]:
            _dispatch_flag("0")  # queue drained — no follow-up link
            break
        _sleep_tick(min(2.0, max(0.1, deadline - time.time())))
    cockpit.stop()
    stop_evt.set()
    return 0


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

    global _ADPS, _HEALTH
    _ADPS = _build_adapters(token, chat)
    if not _ADPS:
        _say(token, chat, "☠ no mirror adapters — check mirrors.json types")
        return 1
    _HEALTH = MirrorHealth([a.name for a in _ADPS])
    seen = "mirrors=%s" % ", ".join(a.name for a in _ADPS)
    print("[brain] mode=%s wake=%02d:%02d:%02d rows=%d %s"
          % (mode, WAKE_HOUR, WAKE_MIN, WAKE_SEC, len(store.students()),
             seen), flush=True)

    if mode == "sweep":
        if _worker_alive(store):
            print("[brain] sweep: a worker is live — silent exit", flush=True)
            return 0
        if _chain_fresh(store):
            print("[brain] sweep: chain fresh — silent exit", flush=True)
            return 0
        try:
            _make_dispatch("chain")()
            _say(token, chat, "♻️ chain dropped — re-seeding a link now")
        except Exception as e:
            print("[brain] sweep: reseed failed %r" % e, flush=True)
        cockpit = Cockpit(token, chat, store, announce=False,
                          run_grind=_make_dispatch("now"))
        cockpit.start()
        time.sleep(int(os.environ.get("BRAIN_SWEEP_SECONDS", "45")))
        cockpit.stop()
        return 0

    if mode == "chain":
        return _chain_session(token, chat, store)

    if mode == "wake":
        _sched_set(store)
        _sleep_until_wake(store)
        _say(token, chat, "⏰ window open %s — grinding %d row(s)"
             % (_now_bdt().isoformat()[11:19],
                len([r for r in store.students() if _owed(r)])))
        cockpit = _NullCockpit() if _worker_alive(store) else Cockpit(
            token, chat, store)
        cockpit.start()
        try:
            _grind_loop(token, chat, store, cockpit)
        finally:
            cockpit.stop()
        return 0

    # mode == "now"
    _sched_set(store)
    cockpit = _NullCockpit() if _worker_alive(store) else Cockpit(
        token, chat, store, announce=False, run_grind=None)
    cockpit.start()
    try:
        _grind_loop(token, chat, store, cockpit)
    finally:
        cockpit.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
