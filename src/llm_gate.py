"""Διακόπτης για τις συνόψεις LLM, με επιβεβαίωση «δεν είμαι ρομπότ» (Cloudflare Turnstile) σε κάθε ενεργοποίηση.

- Οι συνόψεις είναι κλειστές από προεπιλογή. Ο επισκέπτης τις ανοίγει με τον διακόπτη και τικάρει το κουτί.
- Ο server επαληθεύει το Turnstile token στο Cloudflare και δίνει υπογεγραμμένο cookie (2 ώρες). Χωρίς αυτό
  το /api/summary αρνείται — ένα script δεν μπορεί να καλέσει το LLM χωρίς να περάσει από το κουτί.
- Το κλείσιμο σβήνει το cookie· το επόμενο άνοιγμα ζητά ξανά επιβεβαίωση.
- Το κλειδί υπογραφής δημιουργείται τυχαία σε κάθε εκκίνηση: μετά από επανεκκίνηση ξαναζητείται το κουτί.

Ρυθμίσεις: TURNSTILE_SITE_KEY (δημόσιο), TURNSTILE_SECRET_KEY (secret). Με REQUIRE_CAPTCHA=1 (στο Dockerfile)
και χωρίς κλειδιά, οι συνόψεις μένουν κλειστές («fail closed»). Τοπικά χωρίς κλειδιά: χωρίς επιβεβαίωση.
"""

import hashlib
import hmac
import logging
import os
import secrets
import time

import httpx2

log = logging.getLogger("llm_gate")

SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
REQUIRED = os.environ.get("REQUIRE_CAPTCHA") == "1"
VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
COOKIE = "na_llm"
SECONDS = 2 * 3600

_KEY = secrets.token_bytes(32)

def captcha_configured() -> bool:
    return bool(SITE_KEY and SECRET_KEY)


def available() -> bool:
    """Μπορεί να ενεργοποιηθεί ο διακόπτης; (Όχι αν απαιτείται επιβεβαίωση αλλά δεν έχει ρυθμιστεί.)"""
    return captcha_configured() or not REQUIRED


def new_token() -> str:
    expires = str(int(time.time()) + SECONDS)
    return f"{expires}.{hmac.new(_KEY, expires.encode(), hashlib.sha256).hexdigest()}"


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    good = hmac.new(_KEY, expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


def verify(captcha_token: str, ip: str) -> bool:
    """Επαλήθευση στο Cloudflare. Χωρίς ρυθμισμένα κλειδιά (μόνο τοπικά): πάντα επιτυχία."""
    if not captcha_configured():
        return not REQUIRED
    if not captcha_token or len(captcha_token) > 2048:
        return False
    # Μέσω httpx2 (η βιβλιοθήκη του Claude SDK, που λειτουργεί από το Space)· με urllib η σύνδεση HTTPS από το Space
    # προς το Cloudflare «κολλούσε» στη χειραψία. Μία επανάληψη για προσωρινά σφάλματα δικτύου.
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
