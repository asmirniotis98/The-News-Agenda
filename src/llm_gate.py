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
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

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
    data = urllib.parse.urlencode({"secret": SECRET_KEY, "response": captcha_token, "remoteip": ip}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(VERIFY_URL, data=data, method="POST"), timeout=10) as r:
            result = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("Turnstile: αποτυχία επαλήθευσης (%s)", type(e).__name__)
        return False
    if not result.get("success"):
        log.info("Turnstile: απόρριψη (%s)", ",".join(result.get("error-codes", [])) or "—")
    return bool(result.get("success"))
