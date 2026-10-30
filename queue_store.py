# -*- coding: utf-8 -*-
"""Gist KV — the queue + engine state that survive anywhere.

One secret gist, two files:
  queue.json — students in priority order (index 1 tops)
  state.json — polling flags, wake schedule, telegram offset, mirror health

Gist reads/writes need a token with the `gist` scope. Writes replace whole
files via PATCH /gists/:id — trivially cheap at this volume. Transient
API failures retry with backoff before raising.
"""
import json
import threading
import time

import requests

GIST_API = "https://api.github.com/gists/{gid}"
BACKOFF = (1, 2, 4)  # seconds between transient-fail retries
CACHE_TTL = 15       # both files ride ONE GET — cache it to spare the API


class QueueStore:
    def __init__(self, token: str, gist_id: str):
        self.gid = gist_id
        self.lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self._cache = None
        self._cache_ts = 0.0

    # -- low level ---------------------------------------------------------
    def _files(self, fresh: bool = False) -> dict:
        now = time.time()
        if not fresh and self._cache is not None \
                and (now - self._cache_ts) < CACHE_TTL:
            return self._cache
        errs = []
        for wait in (0,) + BACKOFF:
            if wait:
                time.sleep(wait)
            try:
                r = self.s.get(GIST_API.format(gid=self.gid), timeout=20)
                if r.status_code == 200:
                    self._cache = r.json().get("files") or {}
                    self._cache_ts = now
                    return self._cache
                if r.status_code in (429, 500, 502, 503, 504):
                    errs.append("HTTP %s" % r.status_code)
                    continue
                r.raise_for_status()
            except requests.RequestException as e:
                errs.append(type(e).__name__)
        raise RuntimeError("gist %s unreachable (%s)" % (self.gid, "; ".join(errs)))

    def _read(self, fname: str, fresh: bool = False):
        files = self._files(fresh=fresh)
        raw = ((files.get(fname) or {}).get("content") or "").strip()
        if not raw:
            return {}
        return json.loads(raw)

    def _write(self, fname: str, data) -> None:
        blob = json.dumps(data, indent=2, ensure_ascii=False)
        errs = []
        for wait in (0,) + BACKOFF:
            if wait:
                time.sleep(wait)
            try:
                r = self.s.patch(
                    GIST_API.format(gid=self.gid),
                    json={"files": {fname: {"content": blob}}},
                    timeout=20,
                )
                if r.status_code == 200:
                    self._cache = None  # our write changed the whole gist
                    self._cache_ts = 0.0
                    return
                if r.status_code in (429, 500, 502, 503, 504, 403):
                    # 403 is usually the hourly rate limit — retried once
                    # here, surfaced to callers who toast instead of dying
                    errs.append("HTTP %s" % r.status_code)
                    continue
                r.raise_for_status()
            except requests.RequestException as e:
                errs.append(type(e).__name__)
        raise RuntimeError("gist write %s failed (%s)" % (fname, "; ".join(errs)))

    # -- queue -------------------------------------------------------------
    def students(self, fresh: bool = False) -> list:
        with self.lock:
            return self._read("queue.json", fresh=fresh).get("students", [])

    def save_students(self, students: list) -> None:
        with self.lock:
            self._write("queue.json", {"students": students})

    def mutate_students(self, fn) -> list:
        """fn(students) -> students, under the same lock as read+write."""
        with self.lock:
            doc = self._read("queue.json", fresh=True)
            students = fn(doc.get("students", []))
            self._write("queue.json", {"students": students})
            return students

    # -- state -------------------------------------------------------------
    def state(self, fresh: bool = False) -> dict:
        with self.lock:
            return self._read("state.json", fresh=fresh)

    def save_state(self, st: dict) -> None:
        with self.lock:
            self._write("state.json", st)

    def update_state(self, **kw) -> dict:
        with self.lock:
            st = self._read("state.json")
            st.update(kw)
            self._write("state.json", st)
            return st
