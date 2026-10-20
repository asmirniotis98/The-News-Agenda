"""Πρόσβαση με κωδικό: η εφαρμογή ανοίγει μόνο σε όποιον έχει τον κοινό κωδικό (π.χ. φοιτητής και καθηγητής).

- Ο κωδικός δίνεται ως secret ACCESS_CODE (ποτέ στον κώδικα). Χωρίς αυτόν (τοπικά) δεν υπάρχει έλεγχος.
- Με REQUIRE_ACCESS_CODE=1 (στο Dockerfile) η εφαρμογή ΔΕΝ ανοίγει αν ο κωδικός λείπει ή είναι σύντομος
  («fail closed»): ένα λάθος στη ρύθμιση δεν την αφήνει ανοιχτή σε όλους.
- Μετά τη σωστή εισαγωγή, ο browser παίρνει υπογεγραμμένο (HMAC) cookie που λήγει σε 7 ημέρες. Αλλαγή του
  κωδικού ακυρώνει όλα τα cookies.
"""

import hashlib
import hmac
import os
import time

ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
REQUIRED = os.environ.get("REQUIRE_ACCESS_CODE") == "1"
MIN_LENGTH = 12
COOKIE = "na_session"
SESSION_SECONDS = 7 * 24 * 3600

# Σελίδες που φαίνονται χωρίς κωδικό: η σελίδα εισόδου και ό,τι χρειάζεται για να εμφανιστεί.
PUBLIC_PATHS = {"/login", "/logout", "/healthz", "/robots.txt",
                "/static/app.css", "/static/login.js", "/static/ntua-logo.svg"}

_KEY = hashlib.sha256(b"news-agenda-session\x00" + ACCESS_CODE.encode()).digest()


def enabled() -> bool:
    return bool(ACCESS_CODE)


def misconfigured() -> bool:
    """Ζητείται κωδικός αλλά λείπει ή είναι πολύ σύντομος."""
    return (REQUIRED or enabled()) and len(ACCESS_CODE) < MIN_LENGTH


def code_matches(given: str) -> bool:
    # Σύγκριση σε σταθερό χρόνο, ώστε ο χρόνος απάντησης να μη δίνει στοιχεία για τον κωδικό.
    return hmac.compare_digest(hashlib.sha256(given.encode()).digest(),
                               hashlib.sha256(ACCESS_CODE.encode()).digest())


def new_token() -> str:
    expires = str(int(time.time()) + SESSION_SECONDS)
    return f"{expires}.{hmac.new(_KEY, expires.encode(), hashlib.sha256).hexdigest()}"


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    good = hmac.new(_KEY, expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and expires.isdigit() and int(expires) > time.time()


def safe_next(target: str | None) -> str:
    """Μόνο σχετικές διευθύνσεις του ίδιου site (όχι «//άλλο-site»), ώστε η είσοδος να μη γίνεται ανακατεύθυνση αλλού."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"
