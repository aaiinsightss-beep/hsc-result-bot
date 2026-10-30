# -*- coding: utf-8 -*-
"""serve — the bot runs on THIS pc, full cockpit, full queue.

pythonw serve.py  (or start_serve.bat) → hides in the background:

  • Telegram cockpit 24/7 while the pc is on — every button/command
    answers in seconds on the same bot/chat/cloud shares.
  • Queue lives in the same secret gist the cloud uses — one source of
    truth; /add /list /del /top land instantly for both.
  • Heartbeats state.svc_ts every 25s. The cloud checks that beat:
    fresh → cloud stands down (no double polling). Stale ≥90s → the
    on-the-hour watchdog re-seeds a cloud chain link and your pc being
    off/asleep changes nothing.
  • Window-aware grinding: waits for 10:59:55 BDT, then cycles the whole
    queue every 30s (night-throttled to 300s). /run = one immediate
    pass even out of window. /stop closes polling for the day.

Boot reads config.json (telegram creds + optional seed student) and the
gist token via `gh auth token` (falls back to GIST_TOKEN env).
"""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import brain  # noqa: E402
import mirrors  # noqa: E402
import notifier  # noqa: E402
from cockpit import Cockpit  # noqa: E402
from queue_store import QueueStore  # noqa: E402

HEARTBEAT = 510           # svc_ts cadence, budget-safe (100 gist writes/h pooled)
BOOT_RETRY = 45           # crash → toast → wait → reboot inner


def _cfg() -> dict:
    p = BASE / "config.json"
    cfg = json.loads(p.read_text("utf-8-sig"))
    # engine's fill-ins shared with the local shells
    eng = cfg.get("engine") or {}
    cfg.setdefault("student", eng.get("student", cfg.get("student", {})))
    return cfg


def _gist_creds() -> tuple:
    tok = os.environ.get("GIST_TOKEN", "").strip()
    if not tok:
        try:
            tok = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True,
                timeout=15).stdout.strip()
        except Exception as e:
            raise RuntimeError("no GIST_TOKEN env and `gh auth token` "
                               "failed (%s)" % type(e).__name__)
    gid = os.environ.get("GIST_ID", "").strip()
    if not gid:
        cfg = _cfg()
        gid = (cfg.get("gist") or {}).get("id", "")
    if not (tok and gid):
        raise RuntimeError("gist token/id missing — set config.json "
                           "'gist': {'id': ...} or GIST_ID env")
    return tok, gid


def _mill(store: QueueStore, stop_evt: threading.Event) -> None:
    """svc_ts heartbeat thread — the wheel-owner's proof of life."""
    def beat():
        while not stop_evt.is_set():
            try:
                store.update_state(svc_ts=time.time())
            except Exception as e:
                try:
                    with open(os.path.join(BASE, "serve_beat_err.txt"), "a",
                              encoding="utf-8") as fh:
                        fh.write("%s %s: %r\n"
                                 % (time.strftime("%H:%M:%S"),
                                    type(e).__name__, e))
                except Exception:
                    pass
            stop_evt.wait(HEARTBEAT)
    threading.Thread(target=beat, name="mill", daemon=True).start()


def _tg() -> tuple:
    cfg = _cfg()
    t = (cfg.get("telegram") or {}).get("bot_token", "").strip()
    c = str((cfg.get("telegram") or {}).get("chat_id", "")).strip()
    if not (t and c):
        raise RuntimeError("telegram bot_token / chat_id missing in config.json")
    return cfg, t, c


def _override_from_cfg(cfg: dict) -> set:
    """Live-tuning block: GUI/advanced edits apply to the running serve
    at the next cycle — no restart needed."""
    eng = cfg.get("engine") or {}
    if eng.get("cycle_interval"):
        brain.CYCLE_INTERVAL = int(eng["cycle_interval"])
    if eng.get("night_interval"):
        brain.NIGHT_INTERVAL = int(eng["night_interval"])
    if eng.get("mirror_stagger"):
        brain.MIRROR_STAGGER = int(eng["mirror_stagger"])
    return {k for k, m in (cfg.get("mirrors") or {}).items()
            if m.get("enabled")}


def _serve_once() -> None:
    """One inner life: adapters + cockpit + grind. Crash → caller retries."""
    cfg, token, chat = _tg()
    tok, gid = _gist_creds()
    store = QueueStore(tok, gid)
    brain._sched_set(store)

    q = store.students()
    seed = cfg.get("student") or {}
    if not q and seed.get("roll"):
        row = {"name": seed.get("name") or "me",
               "board": (seed.get("board") or "dhaka").lower(),
               "year": str(seed.get("year") or "2026"),
               "roll": str(seed.get("roll") or ""),
               "reg": str(seed.get("reg") or ""),
               "exam": (seed.get("examination") or "ssc").lower()}
        store.mutate_students(lambda st: st + [row])
        q = store.students()
        _tg_say(token, chat, "🌱 queue seeded from config.json (%d row)"
                % len(q))

    adps = brain._build_adapters(token, chat)
    if not adps:
        _tg_say(token, chat, "☠ serve: no mirror adapters — check mirrors.json")
        return
    brain._ADPS = adps
    brain._HEALTH = brain.MirrorHealth([a.name for a in adps])
    health = brain._HEALTH

    stop_evt = threading.Event()
    _mill(store, stop_evt)

    cockpit = Cockpit(token, chat, store, announce=True)
    cockpit.start()
    _tg_say(token, chat, "🖥 serve is up on this pc — buttons live, "
             "window %02d:%02d:%02d BDT · result day %s"
             % (brain.WAKE_HOUR, brain.WAKE_MIN, brain.WAKE_SEC,
                (cfg.get("schedule") or {}).get("target_date") or "every day"))

    enabled_set = _override_from_cfg(_cfg())
    session = mirrors._fresh()
    last_blame = 0.0

    try:
        while True:
            # live-tune: config.json edits land next cycle, no restart
            try:
                cfg_now = _cfg()
                want_enabled = _override_from_cfg(cfg_now)
                if want_enabled != enabled_set:
                    adps = brain._build_adapters(token, chat)
                    if adps:
                        brain._ADPS = adps
                        brain._HEALTH = brain.MirrorHealth(
                            [a.name for a in adps])
                        health = brain._HEALTH
                        enabled_set = want_enabled
                        _tg_say(token, chat, "🔁 serve re-tuned from config: "
                                "%d mirror(s) live" % len(adps))
            except Exception as e:
                print("[serve] re-tune parse failed: %s" % type(e).__name__,
                      flush=True)

            if cockpit.hard_stop.is_set():
                cockpit.hard_stop.clear()
                store.update_state(polling=False,
                                   stop_date=brain._now_bdt().date().isoformat())
                _tg_say(token, chat, "🛑 polling paused — reopens at next "
                        "wake (or /stop again tomorrow 10:59)")

            st = store.state()
            today_iso = brain._now_bdt().date().isoformat()
            # /stop holds only today — the next local day re-opens polling
            if st.get("polling") is False \
                    and (st.get("stop_date") or "") != today_iso:
                store.update_state(polling=True, stop_date="")
                _tg_say(token, chat, "🔓 new day — polling re-opened")
                st = store.state()

            # publish the schedule once per change (state gist) so cloud
            # brains and the GUI/serve share the result day + fire second
            sc_now = cfg_now.get("schedule") or cfg.get("schedule") or {}
            want_sched = {
                "date": str(sc_now.get("target_date") or ""),
                "hour": int(sc_now.get("start_hour_local", brain.WAKE_HOUR)),
                "minute": int(sc_now.get("start_minute_local", brain.WAKE_MIN)),
                "second": int(sc_now.get("start_second_local", brain.WAKE_SEC)),
            }
            if want_sched != getattr(_serve_once, "_sent_sched", None):
                _serve_once._sent_sched = want_sched
                try:
                    store.update_state(schedule=want_sched)
                except Exception:
                    pass
            if want_sched != getattr(_serve_once, "_sent_sched", None):
                _serve_once._sent_sched = want_sched
                try:
                    store.update_state(schedule=want_sched)
                except Exception:
                    pass

            q = store.students()
            pending = [r for r in q if not r.get("found_at")]
            now = brain._now_bdt()
            can = bool(st.get("polling", True)) and pending and \
                brain._wake_open(now)

            if not can:
                if cockpit.run_once.is_set():
                    cockpit.run_once.clear()
                    cockpit.hard_stop.clear()
                    _tg_say(token, chat, "🔬 one out-of-window pass…")
                    brain._one_pass(token, chat, adps, store, health,
                                    session, deliver=True)
                    brain._flush_status(store, health, "serve",
                                        store.students())
                    _tg_say(token, chat,
                            brain._fmt_verdict(store.students(), health))
                brain._sleep_tick(30.0)
                continue

            brain._one_pass(token, chat, adps, store, health, session,
                            deliver=True)
            q = store.students()
            brain._flush_status(store, health, "serve", q)

            if health.consume_rebuild():
                session = mirrors._fresh()

            pending = [r for r in q if not r.get("found_at")]
            if not pending:
                # queue drained — park tight-but-calm; /run warms instantly
                brain._sleep_tick(brain.CYCLE_INTERVAL,
                                  lambda: cockpit.run_once.is_set())
                cockpit.run_once.clear()
                continue

            if not any(not health.cooling(n) for n in health.hc):
                if time.time() - last_blame > brain.BLAME_RATE:
                    last_blame = time.time()
                    _tg_say(token, chat,
                            "🥴 every mirror cooling (%s) — board's dead, "
                            "not you" % ", ".join(
                                "%s: %s" % (n.split(".")[0], h["kind"] or "?")
                                for n, h in health.hc.items()))
                brain._sleep_tick(brain.CYCLE_INTERVAL,
                                  lambda: cockpit.run_once.is_set())
                continue

            ivl = brain.CYCLE_INTERVAL
            if now.hour < 9 or now.hour >= 23:
                ivl = brain.NIGHT_INTERVAL
            brain._sleep_tick(ivl, lambda: cockpit.run_once.is_set()
                              or cockpit.hard_stop.is_set())
            if cockpit.run_once.is_set():
                cockpit.run_once.clear()
    finally:
        cockpit.stop()
        stop_evt.set()


def _tg_say(token: str, chat: str, text: str) -> None:
    brain._say(token, chat, text)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    while True:
        try:
            _, token, chat = _tg()
        except Exception as e:
            print("[serve] config broken: %s" % e, flush=True)
            return 1
        try:
            _serve_once()
            print("[serve] inner loop exited — rebooting in %ds"
                  % BOOT_RETRY, flush=True)
        except KeyboardInterrupt:
            _tg_say(token, chat, "👋 serve stopped by hand")
            return 0
        except Exception as e:
            print("[serve] crash: %s: %s" % (type(e).__name__, e),
                  flush=True)
            try:
                _tg_say(token, chat, "💥 serve crashed (%s) — rebooting in %ds"
                        % (type(e).__name__, BOOT_RETRY))
            except Exception:
                pass
        time.sleep(BOOT_RETRY)


if __name__ == "__main__":
    raise SystemExit(main())
