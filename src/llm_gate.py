"""Διακόπτης για τις συνόψεις LLM, με επιβεβαίωση «δεν είμαι ρομπότ» σε κάθε ενεργοποίηση.

- Οι συνόψεις είναι κλειστές από προεπιλογή. Ο επισκέπτης τις ανοίγει με τον διακόπτη και επιβεβαιώνει.
- Ο server ελέγχει την επιβεβαίωση και δίνει υπογεγραμμένο cookie (2 ώρες). Χωρίς αυτό το /api/summary αρνείται.
- Το κλείσιμο σβήνει το cookie· το επόμενο άνοιγμα ζητά ξανά επιβεβαίωση.
- Το κλειδί υπογραφής δημιουργείται τυχαία σε κάθε εκκίνηση: μετά από επανεκκίνηση ξαναζητείται επιβεβαίωση.

Δύο τρόποι (CAPTCHA_MODE):
- "checkbox" (προεπιλογή): δικό μας κουτί, χωρίς τρίτη υπηρεσία. Ο server δίνει υπογεγραμμένη πρόκληση μίας χρήσης
  (λήγει σε 5 λεπτά) και δέχεται την επιβεβαίωση μόνο αν πέρασε χρόνος ανθρώπου (≥ 1,5 s) και το κρυφό πεδίο-«παγίδα»
  είναι κενό. Πιο αδύναμο από ένα εξωτερικό captcha, αλλά η εφαρμογή ανοίγει ήδη μόνο με κωδικό πρόσβασης και το
  κόστος έχει ημερήσιο όριο.
- "turnstile": Cloudflare Turnstile, επαλήθευση στο Cloudflare (TURNSTILE_SITE_KEY, TURNSTILE_SECRET_KEY). Δεν
  χρησιμοποιείται στο Hugging Face: η σύνδεση του Space προς το challenges.cloudflare.com κόβεται στη χειραψία TLS.

Με REQUIRE_CAPTCHA=1 (στο Dockerfile) και Turnstile χωρίς κλειδιά, οι συνόψεις μένουν κλειστές («fail closed»).
"""

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

import httpx2

log = logging.getLogger("llm_gate")

MODE = os.environ.get("CAPTCHA_MODE", "checkbox")
SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
REQUIRED = os.environ.get("REQUIRE_CAPTCHA") == "1"
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
COOKIE = "na_llm"
SECONDS = 2 * 3600
CHALLENGE_SECONDS = 300   # η πρόκληση του κουτιού λήγει σε 5 λεπτά
MIN_HUMAN_SECONDS = 1.5   # νωρίτερα από αυτό δεν είναι άνθρωπος

_KEY = secrets.token_bytes(32)
_used: dict[str, float] = {}   # προκλήσεις που χρησιμοποιήθηκαν ήδη (nonce → λήξη)
_lock = threading.Lock()


def _sign(text: str) -> str:
    return hmac.new(_KEY, text.encode(), hashlib.sha256).hexdigest()


def captcha_configured() -> bool:
    return MODE == "checkbox" or bool(SITE_KEY and SECRET_KEY)


def available() -> bool:
    """Μπορεί να ενεργοποιηθεί ο διακόπτης; (Όχι αν απαιτείται επιβεβαίωση αλλά δεν έχει ρυθμιστεί.)"""
    return captcha_configured() or not REQUIRED


def client_config() -> dict:
    """Τι χρειάζεται η σελίδα για να δείξει το κουτί."""
    if MODE == "turnstile" and SITE_KEY and SECRET_KEY:
        return {"captcha": "turnstile", "captcha_site_key": SITE_KEY}
    if MODE == "checkbox":
        return {"captcha": "checkbox", "captcha_site_key": None}
    return {"captcha": None, "captcha_site_key": None}


# ---------------------------------------------------------------- cookie «οι συνόψεις είναι ανοιχτές»

def new_token() -> str:
    expires = str(int(time.time()) + SECONDS)
    return f"{expires}.{_sign('llm:' + expires)}"


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    return hmac.compare_digest(sig, _sign("llm:" + expires)) and expires.isdigit() and int(expires) > time.time()


# ---------------------------------------------------------------- δικό μας κουτί

def new_challenge(now: float | None = None) -> str:
    issued = f"{(now if now is not None else time.time()):.3f}"
    nonce = secrets.token_urlsafe(16)
    return f"{issued}.{nonce}.{_sign('challenge:' + issued + ':' + nonce)}"


def check_challenge(challenge: str, confirmed: bool, honeypot: str) -> str | None:
    """None αν η επιβεβαίωση γίνεται δεκτή, αλλιώς η αιτία της απόρριψης (για το log)."""
    if not confirmed:
        return "unchecked"
    if honeypot:
        return "honeypot"
    parts = (challenge or "").split(".")
    if len(parts) != 4:  # issued = «δευτερόλεπτα.χιλιοστά», άρα 4 τμήματα
        return "malformed"
    issued_s, nonce, sig = f"{parts[0]}.{parts[1]}", parts[2], parts[3]
    if not hmac.compare_digest(sig, _sign("challenge:" + issued_s + ":" + nonce)):
        return "bad-signature"
    try:
        age = time.time() - float(issued_s)
    except ValueError:
        return "malformed"
    if age < MIN_HUMAN_SECONDS:
        return "too-fast"
    if age > CHALLENGE_SECONDS:
        return "expired"
    with _lock:
        now = time.time()
        for n in [n for n, exp in _used.items() if exp < now]:
            del _used[n]
        if nonce in _used:
            return "reused"
        _used[nonce] = now + CHALLENGE_SECONDS
    return None


# ---------------------------------------------------------------- Cloudflare Turnstile (εναλλακτικά)

def verify_turnstile(captcha_token: str, ip: str) -> bool:
    if not (SITE_KEY and SECRET_KEY) or not captcha_token or len(captcha_token) > 2048:
        return False
    form = {"secret": SECRET_KEY, "response": captcha_token, "remoteip": ip}
    result = None
    for attempt in (1, 2):
        try:
            r = httpx2.post(VERIFY_URL, data=form, timeout=15.0,
                            headers={"User-Agent": "news-agenda/1.0", "Accept": "application/json"})
            r.raise_for_status()
            result = r.json()
            break
        except (httpx2.HTTPError, ValueError) as e:
            # Η αιτία (π.χ. timeout, HTTP 4xx) — ποτέ το κλειδί ή το token.
            log.warning("Turnstile: αποτυχία επαλήθευσης, προσπάθεια %d (%s: %s)", attempt, type(e).__name__, str(e)[:120])
    if result is None:
        return False
    if not result.get("success"):
        log.info("Turnstile: απόρριψη (%s)", ",".join(result.get("error-codes", [])) or "—")
    return bool(result.get("success"))


def verify(payload: dict, ip: str) -> bool:
    """Ελέγχει την επιβεβαίωση του επισκέπτη σύμφωνα με τον τρόπο που έχει ρυθμιστεί."""
    if MODE == "checkbox":
        reason = check_challenge(str(payload.get("challenge") or ""), payload.get("confirm") is True,
                                 str(payload.get("website") or ""))
        if reason:
            log.info("Κουτί «δεν είμαι ρομπότ»: απόρριψη (%s)", reason)
        return reason is None
    if SITE_KEY and SECRET_KEY:
        return verify_turnstile(str(payload.get("token") or ""), ip)
    return not REQUIRED  # τοπικά, χωρίς κανέναν έλεγχο
