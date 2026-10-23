"""«Φέρτε το δικό σας κλειδί» (BYOK): ο επισκέπτης χρησιμοποιεί το δικό του κλειδί Claude API για νέες συνόψεις.

- Το κλειδί κρατιέται ΜΟΝΟ στη μνήμη του server (μέσα στο client του Anthropic SDK) — ποτέ σε αρχείο, βάση,
  log, cookie, URL ή HTML. Μετά από επανεκκίνηση χάνεται.
- Ο browser παίρνει τυχαίο αναγνωριστικό συνεδρίας (cookie), που δεν σχετίζεται με το κλειδί.
- Η συνεδρία ισχύει 30 λεπτά και μόνο από την IP όπου επικολλήθηκε το κλειδί. Αν αλλάξει η IP, το κλειδί
  σβήνεται αμέσως και πρέπει να επικολληθεί ξανά.
- Ανανέωση για άλλα 30 λεπτά επιτρέπεται μόνο τα τελευταία 5 λεπτά, από την ίδια IP.
- Όριο νέων συνόψεων ανά συνεδρία (BYOK_SESSION_LIMIT), ώστε ένα λάθος να μη χρεώσει τον επισκέπτη πολύ.
"""

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

COOKIE = "na_byok"
SECONDS = 30 * 60
RENEW_WINDOW = 5 * 60
MAX_SESSIONS = 2000   # προστασία μνήμης
KEY_PATTERN = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,250}$")


class KeyRequired(Exception):
    """Χρειάζεται (νέο) κλειδί API· το μήνυμα εξηγεί γιατί."""


@dataclass
class Session:
    client: Any
    ip: str
    expires: float
    used: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def looks_like_key(key: str) -> bool:
    return bool(KEY_PATTERN.match(key))


def _id(token: str) -> str:
    # Στη μνήμη κρατάμε μόνο το hash του αναγνωριστικού, όχι το ίδιο το cookie.
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, limit: int, clock: Callable[[], float] = time.time):
        self.limit = limit
        self.clock = clock
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()

    def _purge(self, now: float) -> None:
        for sid in [s for s, v in self.sessions.items() if v.expires <= now]:
            del self.sessions[sid]

    def create(self, client: Any, ip: str) -> str | None:
        """Νέα συνεδρία· επιστρέφει το token του cookie (None αν η μνήμη είναι γεμάτη)."""
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            self._purge(now)
            if len(self.sessions) >= MAX_SESSIONS:
                return None
            self.sessions[_id(token)] = Session(client, ip, now + SECONDS)
        return token

    def get(self, token: str | None, ip: str) -> Session:
        """Η ενεργή συνεδρία του επισκέπτη, αλλιώς KeyRequired με την αιτία."""
        if not token:
            raise KeyRequired("Για νέες συνόψεις χρειάζεται το δικό σας κλειδί Claude API.")
        sid = _id(token)
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                raise KeyRequired("Το κλειδί σας δεν είναι πια ενεργό. Επικολλήστε το ξανά.")
            if s.expires <= self.clock():
                del self.sessions[sid]
                raise KeyRequired("Τα 30 λεπτά έληξαν και το κλειδί σβήστηκε. Επικολλήστε το ξανά.")
            if s.ip != ip:
                del self.sessions[sid]
                raise KeyRequired("Η σύνδεσή σας άλλαξε (νέα IP), οπότε το κλειδί σβήστηκε. Επικολλήστε το ξανά.")
            return s

    def status(self, token: str | None, ip: str) -> dict:
        try:
            s = self.get(token, ip)
        except KeyRequired as e:
            return {"active": False, "reason": str(e) if token else None}
        return {"active": True, "expires_in": int(s.expires - self.clock()), "renew_window": RENEW_WINDOW,
                "used": s.used, "limit": self.limit}

    def renew(self, token: str | None, ip: str) -> int:
        """Άλλα 30 λεπτά· μόνο στα τελευταία 5. Επιστρέφει τα δευτερόλεπτα που απομένουν."""
        s = self.get(token, ip)
        with self.lock:
            left = s.expires - self.clock()
            if left > RENEW_WINDOW:
                raise ValueError("Η ανανέωση είναι δυνατή μόνο τα τελευταία 5 λεπτά.")
            s.expires = self.clock() + SECONDS
            return SECONDS

    def forget(self, token: str | None) -> None:
        if token:
            with self.lock:
                self.sessions.pop(_id(token), None)

    def drop(self, session: Session) -> None:
        """Σβήνει μια συνεδρία (π.χ. όταν το Anthropic απέρριψε το κλειδί)."""
        with self.lock:
            for sid in [k for k, v in self.sessions.items() if v is session]:
                del self.sessions[sid]

    def spend(self, session: Session) -> None:
        """Μετράει μία νέα σύνοψη στη συνεδρία (BudgetExceeded πάνω από το όριο)."""
        from src.summarize import BudgetExceeded

        with session.lock:
            if session.used >= self.limit:
                raise BudgetExceeded(f"Φτάσατε το όριο των {self.limit} νέων συνόψεων για αυτή τη συνεδρία. "
                                     "Επικολλήστε ξανά το κλειδί για να συνεχίσετε.")
            session.used += 1


# ---------------------------------------------------------------- κλειδί → client

def make_client(key: str):
    import anthropic

    return anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)


def validate(client, model: str) -> str | None:
    """Ελάχιστη, δωρεάν κλήση (στοιχεία του μοντέλου). None αν το κλειδί δουλεύει, αλλιώς μήνυμα για τον επισκέπτη.
    Τα μηνύματα δεν περιέχουν ποτέ το κλειδί ή το κείμενο του σφάλματος του Anthropic."""
    import anthropic

    try:
        client.with_options(timeout=15.0, max_retries=1).models.retrieve(model)
    except anthropic.AuthenticationError:
        return "Το κλειδί δεν έγινε δεκτό από το Anthropic. Ελέγξτε ότι το αντιγράψατε ολόκληρο."
    except (anthropic.PermissionDeniedError, anthropic.NotFoundError):
        return f"Το κλειδί δεν έχει πρόσβαση στο μοντέλο {model}."
    except anthropic.RateLimitError:
        return "Ο λογαριασμός σας στο Anthropic έφτασε το όριο αιτημάτων. Δοκιμάστε σε λίγο."
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        return "Δεν ήταν δυνατός ο έλεγχος του κλειδιού αυτή τη στιγμή. Δοκιμάστε ξανά."
    return None
