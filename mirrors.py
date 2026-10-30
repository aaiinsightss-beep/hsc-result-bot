# -*- coding: utf-8 -*-
"""Mirror adapters — the 2026 portal contract (educationboardresults + eboard).

Both boards run the SAME webapp now:
  1. first-party POW gate  : SHA-256("browser-proof:<seed>:<salt>") -> POST /_challenge-verify
  2. portal form           : root or /v2/home, pulls captcha_svc_url + result_svc_url
  3. captcha               : GET <captcha_svc_url>?t=<ms> -> PNG -> OCR
  4. result                : POST <result_svc_url> (-urlencoded, AJAX) -> JSON
                             {status, msg, res:{name,roll_no,regno,board_name,session,
                                                res_detail,display_details,...}}

The challenge is the portal's OWN published hashing algorithm — computed
client-side identically in their inline JS; nothing here is hidden.
"""
import json
import re
import hashlib
import datetime
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ocr_captcha import decode as ocr_decode

NOT_PUBLISHED_MARKERS = (
    "not published yet",
    "not published",
    "no result found",
    "information does not match",
    "no data found",
    "result not found",
    "wrong captcha",
    "captcha mismatch",
)

CHALLENGE_MARK = "Verifying you are not a robot"

# national subject-code table — codes come from the grade CSV; names only
# decorate the telegram card, unknown codes stay literal
SUBJECT_NAMES = {
    "100": "Bangla (comp)", "101": "Bangla (1st)", "102": "Bangla (2nd)",
    "103": "Bangla A&V", "107": "English (1st)", "108": "English (2nd)",
    "109": "General Math", "110": "Religion", "111": "BGS", "112": "Science",
    "121": "Economics", "126": "General Science", "127": "HSC Science",
    "129": "Agriculture", "132": "Civics", "133": "Geography",
    "136": "Chemistry", "138": "Biology", "141": "H-History",
    "147": "Higher Math", "148": "Social Science", "150": "History",
    "154": "Physics", "156": "Statistics", "158": "H-Political Science",
    "163": "Psychology", "170": "Agricultural Studies",
    "174": "Engineering", "175": "Business Law", "176": "Accounting",
    "177": "Physics (HSC)", "178": "Biology (HSC)", "179": "Chemistry (HSC)",
    "181": "Statistics (HSC)", "187": "Finance", "188": "Production",
    "190": "Economics (HSC)", "191": "Civics (HSC)", "192": "Accounting (HSC)",
    "200": "Agriculture (HSC)", "238": "Cultural Studies",
    "239": "Statistics (HSC-2)", "240": "Social Work", "241": "History (HSC)",
    "242": "Geography (HSC)", "243": "Economics (HSC-2)",
    "264": "Linguistics", "265": "Islamic Studies", "267": "Islamic History",
    "269": "Education", "270": "Islamic Education", "271": "A&I Studies",
    "275": "ICT", "282": "Statistics (HSC-3)", "285": "Physics (HSC-2)",
    "286": "Finance (HSC)", "287": "Chemistry (HSC-2)", "288": "Biology (HSC-2)",
    "289": "Statistics (HSC-4)", "293": "Bangla (Old Syllabus)",
    "240": "Social Work", "130": "Physical Education",
}


class NotPublished(Exception):
    pass


class ParseError(Exception):
    pass


def _fresh():
    import cloudscraper
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "desktop": True}
    )
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _base(url):
    u = urlparse(url)
    return "%s://%s" % (u.scheme, u.netloc)


def _looks_unpublished(text: str) -> bool:
    low = str(text).lower()
    return any(marker in low for marker in NOT_PUBLISHED_MARKERS)


class Portal:
    """Shared adapter for the 2026 portal markup on both education boards."""

    def __init__(self, url, name, timeout=20):
        self.url = url.rstrip("/")
        self.name = name
        self.timeout = timeout
        self.captcha_bytes = None
        self.result_url = None
        self.captcha_url = None
        self.result_method = "post"
        self.form_payload = {}

    # -- POW gate ---------------------------------------------------------
    def _challenge_get(self, s, path=None):
        """GET path through the POW gate, solving up to 3 rounds."""
        target = path or self.url
        r = s.get(target, timeout=self.timeout)
        for _ in range(3):
            if CHALLENGE_MARK not in r.text[:500]:
                return r
            m = re.search(r"var challenge = (\{.*?\});", r.text, re.S)
            d = re.search(r'var destination = "([^"]+)"', r.text)
            if not m:
                raise ParseError("challenge payload not found")
            p = json.loads(m.group(1))
            ans = "hash:" + hashlib.sha256(
                ("browser-proof:%s:%s" % (p["seed"], p["salt"])).encode()
            ).hexdigest()
            base = _base(r.url)
            vr = s.post(
                urljoin(base, "/_challenge-verify"),
                json={"token": p["token"], "answer": ans},
                timeout=15,
                headers={"Content-Type": "application/json",
                         "Referer": r.url, "Origin": base},
            )
            vr.raise_for_status()
            if not vr.json().get("ok"):
                raise ParseError("challenge-verify rejected our proof")
            time.sleep(0.6)
            r = s.get(
                urljoin(base, d.group(1) if d else "/"),
                timeout=self.timeout,
                headers={"Referer": base},
            )
        if CHALLENGE_MARK in r.text[:500]:
            raise ParseError("bot-check gate persisted past 3 rounds")
        return r

    # -- adapter interface -------------------------------------------------
    def prepare(self, s):
        page = self._challenge_get(s)
        soup = BeautifulSoup(page.text, "lxml")
        form = soup.find("form")
        if form is None:
            raise ParseError("no result form found on passed page")

        payload = {}
        for inp in form.find_all("input"):
            nm = inp.get("name")
            if nm:
                payload[nm] = inp.get("value", "")
        for sel in form.find_all("select"):
            nm = sel.get("name")
            if nm and nm not in payload:
                payload[nm] = ""

        # service endpoints live as inline JS vars
        m = re.search(r"var\s+captcha_svc_url\s*=\s*'([^']+)'", page.text)
        self.captcha_url = m.group(1) if m else "/v2/captcha"
        m = re.search(r"var\s+result_svc_url\s*=\s*'([^']+)'", page.text)
        if not m:
            raise ParseError("result service url missing from page JS")
        self.result_url = m.group(1)
        m = re.search(r"var\s+result_svc_method\s*=\s*'([^']+)'", page.text)
        self.result_method = (m.group(1).lower() if m else "post")

        self._grab_captcha(s)
        return payload

    def _grab_captcha(self, s):
        """Fresh captcha PNG from the portal (session-tied)."""
        base = _base(self.url)
        cr = s.get(
            urljoin(base + "/", self.captcha_url.lstrip("/")),
            params={"t": int(time.time() * 1000)},
            timeout=self.timeout,
            headers={"Referer": self.url},
        )
        cr.raise_for_status()
        if len(cr.content) < 500:
            raise ParseError("captcha fetch failed (%d bytes)" % len(cr.content))
        self.captcha_bytes = cr.content

    def submit(self, s, cfg, payload, captcha):
        st = cfg["student"]
        data = {
            "exam": st["examination"],
            "board": st["board"],
            "year": st["year"],
            "result_type": "1",
            "roll": st["roll"],
            "reg": st["reg"],
            "eiin": "",
            "dcode": "",
            "ccode": "",
            "captcha": captcha,
            "submit": "View Result",
        }
        for k, v in payload.items():
            data.setdefault(k, v)
        base = _base(self.url)
        r = s.post(
            urljoin(base + "/", self.result_url.lstrip("/")),
            data=data,
            timeout=self.timeout,
            headers={
                "Referer": self.url,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
        )
        r.raise_for_status()
        return r.text

    def parse(self, body):
        try:
            resp = json.loads(body)
        except ValueError:
            if _looks_unpublished(body):
                raise NotPublished()
            raise ParseError("result svc returned non-JSON page")

        if resp.get("status") != 0:
            msg = str(resp.get("msg") or "unknown error")
            if _looks_unpublished(msg):
                raise NotPublished()
            if "captcha" in msg.lower():
                raise ParseError("captcha rejected by server")
            raise ParseError("site msg: %s" % msg)

        res = resp.get("res") or {}
        grades = []
        for src in ("display_details", "display_details_ca"):
            blob = res.get(src)
            if not blob:
                continue
            suffix = " (CA)" if src.endswith("_ca") else ""
            for item in str(blob).split(","):
                if ":" not in item:
                    continue
                code_part, grade_part = item.split(":", 1)
                # optional marks tail: '=' theory,practical,mcq,total
                grade = grade_part.split("=")[0].strip()
                code = code_part.strip()
                sub = SUBJECT_NAMES.get(code, "Code %s" % code)
                grades.append(("%s%s" % (sub, suffix), grade))

        result = {
            "name": res.get("name") or "",
            "roll": str(res.get("roll_no") or ""),
            "reg": str(res.get("regno") or ""),
            "board": res.get("board_name") or "",
            "session": res.get("session") or "",
            "group": res.get("stud_group") or "",
            "dob": res.get("dob") or "",
            "institute": res.get("inst_name") or "",
            "res_detail": res.get("res_detail") or "",
            "gpa": "Passed" if res.get("res_detail") == "P" else (res.get("res_detail") or ""),
            "grades": grades,
            "stat_url": resp.get("stat_url") or "",
            "showmarks": resp.get("showmarks"),
        }
        if not result["name"] and not grades:
            raise ParseError("result card empty in JSON response")
        return result


ADAPTERS = {"portal": Portal, "eduboard": Portal, "eboard": Portal}


class DibdPortal:
    """www.educationboard.gov.bd — the Ministry's own classic portal.

    Different contract from the 2026 portals: no POW gate, no PNG
    captcha — a plain math check ("6 + 1") whose operands ship as
    HIDDEN FORM FIELDS, solved client-side. POST result.php multipart:

        sr / et (hidden page ids), exam, year, board, roll, reg,
        value_a / value_b / value_s

    Board replies as plain text ("RESULT NOT FOUND!", "RESULT IS NOT
    PUBLISHED YET!", "Entered value does not match!") or an HTML card.
    """

    def __init__(self, url, name, timeout=20):
        self.url = url.rstrip("/")
        self.name = name
        self.timeout = timeout
        self.captcha_bytes = None       # no PNG captcha on this site
        self.result_url = None
        self.captcha_url = None
        self.result_method = "post"
        self.form_payload = {}
        self.math_captcha = True        # pull() routes to the no-OCR branch
        self._va = "0"
        self._vb = "0"

    # reuse the shared POW-gate getter — this site has no gate, the call
    # plain-GETs the form page and returns it untouched
    _challenge_get = Portal._challenge_get

    def _grab_captcha(self, s):
        pass  # math captcha lives in the form page itself

    def prepare(self, s):
        page = self._challenge_get(s)
        soup = BeautifulSoup(page.text, "lxml")
        try:
            div = soup.find(id="captcha")
            self._va = soup.find(id="value_a")["value"] or "0"
            self._vb = soup.find(id="value_b")["value"] or "0"
        except (AttributeError, TypeError) as e:
            raise ParseError("educationboard.gov.bd form page unreadable "
                             "(%s)" % type(e).__name__)
        if not div or not (self._va.isdigit() and self._vb.isdigit()):
            raise ParseError("math captcha fields missing on form page")
        return {}

    def submit(self, s, cfg, payload, captcha):
        st = cfg["student"]
        va, vb = int(self._va), int(self._vb)
        data = {
            "sr": "1", "et": "2",
            "exam": (st.get("examination") or "ssc").lower(),
            "year": str(st.get("year") or ""),
            "board": st.get("board") or "",
            "roll": str(st.get("roll") or ""),
            "reg": str(st.get("reg") or ""),
            "value_a": str(va), "value_b": str(vb),
            "value_s": str(va + vb),
        }
        # multipart/form-data — their JS posts FormData (contentType:false)
        files = {k: (None, str(v)) for k, v in data.items()}
        r = s.post(self.url.rstrip("/") + "/result.php", files=files,
                   timeout=self.timeout, headers={"Referer": self.url})
        r.raise_for_status()
        return r.text

    _GRADE_TOK = re.compile(
        r"^(A\+|A|A-|A1|B\+|B|B-|C\+|C|D|F|A\*|PASS|FAILED?)$", re.I)

    def parse(self, body):
        low = str(body).lower()
        if "entered value does not match" in low:
            raise ParseError("math captcha rejected by server")
        if _looks_unpublished(low):
            raise NotPublished()
        soup = BeautifulSoup(body, "lxml")
        text = soup.get_text("\n", strip=True)
        if _looks_unpublished(text):
            raise NotPublished()

        kv = {}
        grades = []
        for tr in soup.find_all("tr"):
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            tds = [t for t in tds if t]
            if len(tds) == 2:
                a, b = tds
                lb = a.rstrip(": ").strip().lower()
                if self._GRADE_TOK.match(b.strip()) and lb and \
                        "grade" not in lb and "subject" not in lb:
                    grades.append((a.rstrip(": "), b.strip().upper()))
                elif lb in ("student name", "name"):
                    kv.setdefault("name", b)
                elif "roll" in lb and len(lb) < 14:
                    kv.setdefault("roll", b)
                elif ("reg" in lb or "registration" in lb):
                    kv.setdefault("reg", b)
                elif "board" in lb and len(lb) < 16:
                    kv.setdefault("board", b)
                elif "institute" in lb or "centre" in lb:
                    kv.setdefault("institute", b)
                elif "gpa" in lb or "grade point average" in lb \
                        or lb == "result":
                    kv.setdefault("gpa", b)
        if not kv.get("name") and not grades:
            raise ParseError("educationboard.gov.bd card had no parseable "
                             "rows — markup changed?")
        return {
            "name": kv.get("name", ""),
            "roll": str(kv.get("roll", "")),
            "reg": str(kv.get("reg", "")),
            "board": kv.get("board", ""),
            "session": "",
            "group": "",
            "dob": "",
            "institute": kv.get("institute", ""),
            "res_detail": kv.get("gpa", "") or "",
            "gpa": kv.get("gpa", "") or "Passed",
            "grades": grades,
            "stat_url": "",
            "showmarks": None,
        }


ADAPTERS = {"portal": Portal, "eduboard": Portal, "eboard": Portal,
            "dibd": DibdPortal}


def build(mirror_cfg, timeout):
    name = mirror_cfg.get("name") or ""
    cls = ADAPTERS.get(mirror_cfg["type"])
    if cls is None:
        raise ParseError("unknown mirror type %r" % mirror_cfg["type"])
    return cls(mirror_cfg["url"], name, timeout)


def pull(adp, cfg, session=None, on_status=None):
    """One full attempt on one mirror -> result dict.

    Captcha OCR misses are routine — image refreshes per request server-side,
    so decode fails scrub in-loop up to 4 fresh captcha grabs before giving
    the attempt back to the engine's 30s cycle.
    """
    s = session or _fresh()
    if on_status:
        on_status("bot-check %s" % adp.name)
    payload = adp.prepare(s)
    if getattr(adp, "math_captcha", False):
        # text-math portals (educationboard.gov.bd) — the answer is solved
        # inside prepare(); no PNG, no OCR loop, one submit
        body = adp.submit(s, cfg, payload, None)
    else:
        captcha = ""
        best_transient = ""
        for attempt in range(6):
            if on_status and attempt:
                on_status("captcha retry %d" % attempt)
            try:
                captcha = ocr_decode(adp.captcha_bytes)
            except RuntimeError:
                captcha = ""
            if len(captcha) == 4:
                break
            # sub-4 reads are noise — keep the best as last resort, regrab
            if len(captcha) > len(best_transient):
                best_transient = captcha
            captcha = ""
            adp._grab_captcha(s)
        if not captcha:
            captcha = best_transient
        if not captcha:
            raise ParseError("captcha OCR never landed")
        body = adp.submit(s, cfg, payload, captcha)
    if _looks_unpublished(body):
        raise NotPublished()
    res = adp.parse(body)
    res["found_at_local"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return res
