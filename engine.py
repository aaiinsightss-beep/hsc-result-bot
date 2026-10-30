# -*- coding: utf-8 -*-
"""Engine — shared core for both shells (CLI main.py, GUI backend.py).

Loop shape: every interval_seconds, stagger enabled mirrors ~5s apart,
first successful parse wins -> Telegram -> shutdown.
Bot-check + captcha + submit + parse all live in mirrors.py.
"""
import json
import threading
import time
import datetime
from pathlib import Path

import mirrors
import notifier
from mirrors import build, NotPublished, ParseError

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "config.json missing next to engine.py — copy the template over"
        )
    return json.loads(CONFIG_PATH.read_text("utf-8-sig"))


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def resolve_tz(name: str, log=None):
    """Named zone or machine-local fallback — never silent UTC on Windows.

    Windows without the tzdata wheel has no zoneinfo database;
    ZoneInfo(name) raises and the old code degraded to UTC, which skews
    the whole calendar +6h on a BDT machine. Machine-local is the right
    fallback: schedule times are written from this machine's clock.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        if log:
            log("tz %r unavailable — using machine-local zone" % name, "err")
        try:
            return datetime.datetime.now().astimezone().tzinfo
        except Exception:
            return datetime.timezone.utc


def validate(cfg: dict) -> list:
    problems = []
    st = cfg.get("student", {})
    if not st.get("roll"):
        problems.append("student.roll empty")
    if not st.get("reg"):
        problems.append("student.reg empty")
    if not st.get("board"):
        problems.append("student.board empty")
    tg = cfg.get("telegram", {})
    if not tg.get("bot_token"):
        problems.append("telegram.bot_token empty")
    if not tg.get("chat_id"):
        problems.append("telegram.chat_id empty")
    enabled = [k for k, m in cfg.get("mirrors", {}).items() if m.get("enabled")]
    if not enabled:
        problems.append("no mirror enabled")
    return problems


def countdown_target(cfg: dict, force_now: bool = False) -> dict:
    """Resolve the fire moment.

    Precedence: schedule.target_date (exact calendar day) > force_now >
    nearest 10:59:55 (today if still ahead, else tomorrow).
    """
    sc = cfg.get("schedule", {})
    tzname = sc.get("timezone", "Asia/Dhaka")
    try:
        tz = resolve_tz(tzname)
    except Exception:
        tz = datetime.timezone.utc

    hour = int(sc.get("start_hour_local", 10))
    minute = int(sc.get("start_minute_local", 59))
    second = int(sc.get("start_second_local", 55))

    if force_now:
        target = datetime.datetime.now(tz) + datetime.timedelta(seconds=10)
        return {"iso": target.isoformat(), "timezone": tzname}

    now = datetime.datetime.now(tz)

    datestr = (sc.get("target_date") or "").strip()
    if datestr:
        try:
            d = datetime.date.fromisoformat(datestr)
            target = datetime.datetime.combine(
                d, datetime.time(hour, minute, second), tzinfo=tz
            )
            if target > now:
                return {"iso": target.isoformat(), "timezone": tzname}
            # date is in the past — fall through to nearest-10:59:55
        except ValueError:
            pass  # malformed date — fall through

    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return {"iso": target.isoformat(), "timezone": tzname}


class Engine:
    def __init__(self, log_cb=None):
        self.log = log_cb or (lambda msg, tag="info": print("[%s] %s" % (tag, msg)))
        self.stop = threading.Event()
        self.done = threading.Event()
        self.result_sent = False
        self._thread = None
        self.config = load_config()

    # ----- lifecycle -----------------------------------------------------
    def start(self, force_now: bool = False) -> bool:
        if self._thread and self._thread.is_alive() and not self.stop.is_set():
            self.log("already running", "err")
            return False
        problems = validate(self.config)
        if problems:
            for p in problems:
                self.log("config: %s" % p, "err")
            return False
        self.stop.clear()
        self.done.clear()
        self._thread = threading.Thread(
            target=self._run, args=(force_now,), daemon=True
        )
        self._thread.start()
        return True

    def shutdown(self) -> None:
        self.stop.set()

    # ----- test ---------------------------------------------------------
    def test(self) -> dict:
        cfg = self.config
        out = {}
        timeout = cfg["retry"].get("request_timeout_seconds", 20)
        for key, m in cfg.get("mirrors", {}).items():
            try:
                r = mirrors._fresh().get(m["url"], timeout=timeout)
                ok = r.status_code == 200
                out[key] = {"status": r.status_code, "ok": ok}
                self.log("test %-28s %s" % (key, "up %s" % r.status_code if ok
                                            else "HTTP %s" % r.status_code),
                         "ok" if ok else "err")
            except Exception as e:
                out[key] = {"status": None, "ok": False, "err": type(e).__name__}
                self.log("test %-28s down (%s)" % (key, type(e).__name__), "err")
        problems = validate(cfg)
        if problems:
            self.log("config incomplete: %s" % "; ".join(problems), "err")
            out["config"] = False
        else:
            try:
                ok = notifier.send_ping(cfg, "🔌 HSC bot wired — engine test OK")
                out["telegram"] = ok
                self.log("test telegram %s" % ("ok" if ok else "send error"), "ok" if ok else "err")
            except Exception as e:
                out["telegram"] = False
                self.log("test telegram down (%s)" % type(e).__name__, "err")
        return out

    # ----- the loop -----------------------------------------------------
    def _run(self, force_now: bool) -> None:
        cfg = self.config
        interval = int(cfg["retry"].get("interval_seconds", 30))
        timeout = int(cfg["retry"].get("request_timeout_seconds", 20))
        tgt = countdown_target(cfg, force_now=force_now)
        self.log("⏳ armed for %s (%s)" % (tgt["iso"][:19], tgt["timezone"]), "wait")

        while not self.stop.is_set():
            # compare in the schedule's timezone to bridge DST/etc.
            tz = resolve_tz(tgt["timezone"])
            now = datetime.datetime.now(tz)
            target = datetime.datetime.fromisoformat(tgt["iso"])
            if target.tzinfo is None:
                target = target.replace(tzinfo=tz)
            if now < target:
                remain = (target - now).total_seconds()
                self.stop.wait(min(1.0, remain))
                continue
            break
        self.log("🚀 firing window open", "info")

        adapters = []
        for key, m in cfg.get("mirrors", {}).items():
            if not m.get("enabled"):
                continue
            try:
                adapters.append(build(m, timeout))
            except Exception as e:
                self.log("adapter %s broken (%s)" % (key, e), "err")

        # ONE warm session for the whole pull phase — CF keeps cookies warm
        session = mirrors._fresh()
        cycle = 0
        while not self.stop.is_set() and not self.result_sent:
            cycle += 1
            for i, adp in enumerate(adapters):
                if self.stop.is_set() or self.result_sent:
                    break
                if i:  # stagger second/third mirror ~5s after the first
                    self.stop.wait(5)
                try:
                    res = mirrors.pull(adp, cfg, session=session, on_status=self.log)
                except NotPublished:
                    self.log("⏳ %s — not published yet" % adp.name, "pause")
                    continue
                except ParseError as e:
                    self.log("🔁 %s — %s" % (adp.name, e), "info")
                    continue
                except Exception as e:
                    self.log("⏱ %s — %s: %s" % (adp.name, type(e).__name__, e), "err")
                    continue
                # WIN — parsed result card
                self.log("✅ result parsed via %s (cycle %d)" % (adp.name, cycle), "ok")
                self.result_sent = True
                try:
                    notifier.send_result(cfg, res)
                    self.log("📣 telegram delivered — done here either way", "ok")
                except Exception as e:
                    self.log("telegram send failed: %s" % e, "err")
                break
            if not self.result_sent and not self.stop.is_set():
                self.stop.wait(interval)

        self.log("🛑 bot stopped" + (" — result delivered" if self.result_sent else ""), "info")
        self.done.set()
