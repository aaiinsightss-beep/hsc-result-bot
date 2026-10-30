# -*- coding: utf-8 -*-
"""Telegram delivery — sendMessage with markdown, plain-text fallback."""
import re
import requests

API = "https://api.telegram.org/bot{token}/{method}"


def _clean(text: str) -> str:
    return re.sub(r"\[\(|\)\]|<<|>>", "", text)


def send_result(cfg: dict, result: dict) -> bool:
    """Push the parsed result card. Returns True on any accepted send."""
    token = cfg["telegram"]["bot_token"].strip()
    chat = cfg["telegram"]["chat_id"].strip()
    if not token or not chat:
        raise ValueError("telegram bot_token / chat_id missing in config.json")

    exam = str((cfg.get("student", {}) or {}).get("examination") or "result").upper()
    lines = ["🎓 *%s RESULT* 🎉" % exam, ""]
    for key in ("name", "roll", "reg", "board", "year"):
        if result.get(key):
            lines.append("*%s:* %s" % (key.title(), result[key]))
    if result.get("gpa") is not None:
        lines.append("*GPA:* %s" % result["gpa"])
    grades = result.get("grades") or []
    if grades:
        lines.append("")
        lines.append("*Subjects*")
        for subj, grade in grades:
            lines.append("• %s — %s" % (subj, grade))
    lines.append("")
    lines.append("⏱ %s" % result.get("found_at_local", ""))

    text = "\n".join(lines)
    r = requests.post(
        API.format(token=token, method="sendMessage"),
        json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
        timeout=15,
    )
    if r.ok and r.json().get("ok"):
        return True
    # markdown parse failures fall back to plain text so the alert always lands
    r = requests.post(
        API.format(token=token, method="sendMessage"),
        json={"chat_id": chat, "text": _clean(text)},
        timeout=15,
    )
    if r.ok and r.json().get("ok"):
        return True
    raise RuntimeError("telegram send failed: %s %s" % (r.status_code, r.text[:200]))


def send_ping(cfg: dict, text: str) -> bool:
    """Plain message used by --test."""
    token = cfg["telegram"]["bot_token"].strip()
    chat = cfg["telegram"]["chat_id"].strip()
    r = requests.post(
        API.format(token=token, method="sendMessage"),
        json={"chat_id": chat, "text": text},
        timeout=15,
    )
    return bool(r.ok and r.json().get("ok"))
