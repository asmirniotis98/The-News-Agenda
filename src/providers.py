"""Πάροχοι LLM για τις συνόψεις: Claude (Anthropic, προεπιλογή) και ChatGPT (OpenAI).

Ο επισκέπτης διαλέγει πάροχο και μοντέλο από σταθερή λίστα (MODELS) και επικολλά το δικό του κλειδί (src/byok.py).
Ο server δέχεται μόνο μοντέλα της λίστας, ώστε κανείς να μη ζητήσει άλλο (π.χ. ακριβότερο) μοντέλο.

Κάθε πάροχος:
- complete(system, user, schema) → κείμενο JSON με τις κουκκίδες,
- validate() → None αν το κλειδί δουλεύει (δωρεάν κλήση: στοιχεία του μοντέλου), αλλιώς μήνυμα για τον επισκέπτη.

Ασφάλεια:
- Οι διευθύνσεις των παρόχων είναι σταθερές εδώ — ο επισκέπτης δεν ορίζει ποτέ URL.
- Το κλειδί υπάρχει μόνο μέσα στο αντικείμενο του παρόχου, στη μνήμη. Μηνύματα σφάλματος και logs δεν περιέχουν
  ποτέ το κλειδί ούτε το σώμα της απάντησης του παρόχου (μόνο κωδικό HTTP και τύπο σφάλματος).

Μοντέλα και τιμές επιβεβαιώθηκαν στην τεκμηρίωση των παρόχων στις 23/10/2026. Κόστος ανά σύνοψη: εκτίμηση με τη
μέτρηση του Haiku 5.5 στο Space (~5.500 tokens είσοδος, ~450 έξοδος)· ο λόγος κάθε μοντέλου μπορεί να προσθέσει.
"""

import json
import logging
import os
import re

import httpx2

log = logging.getLogger("providers")


class SummaryUnavailable(Exception):
    """Δεν υπάρχει κλειδί API ή το LLM δεν απάντησε."""


class NoClient(SummaryUnavailable):
    """Χρειάζεται νέα σύνοψη αλλά δεν υπάρχει κλειδί API."""


class KeyRejected(SummaryUnavailable):
    """Ο πάροχος απέρριψε το κλειδί (άκυρο, ανακλήθηκε ή χωρίς υπόλοιπο)."""


# ---------------------------------------------------------------- κατάλογος: πάροχοι και μοντέλα

INFO = {
    "anthropic": {
        "label": "Claude", "company": "Anthropic",
        "pattern": r"^sk-ant-[A-Za-z0-9_\-]{20,250}$", "hint": "sk-ant-…",
        "console": "https://console.anthropic.com/settings/keys",
        "models": [  # (id, όνομα, $ είσοδος / έξοδος ανά 1M tokens, εκτίμηση ανά σύνοψη)
            {"id": "claude-haiku-5-5", "name": "Claude Haiku 5.5", "price": "$0,10 / $0,50", "per_summary": "~$0,001",
             "note": "γρήγορο και φθηνό — επαρκεί για συνόψεις τίτλων"},
            {"id": "claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "price": "$2 / $10", "per_summary": "~$0,016",
             "note": "ισορροπία ποιότητας και κόστους"},
            {"id": "claude-opus-5-5", "name": "Claude Opus 5.5", "price": "$4 / $20", "per_summary": "~$0,03",
             "note": "το πιο ικανό — ακριβότερο και πιο αργό"},
        ],
    },
    "openai": {
        "label": "ChatGPT", "company": "OpenAI",
        "pattern": r"^sk-(?!ant-)[A-Za-z0-9_\-]{20,250}$", "hint": "sk-proj-…",
        "console": "https://platform.openai.com/api-keys",
        "models": [
            {"id": "gpt-5.4-nano", "name": "GPT-5.4 nano", "price": "$0,20 / $1,25", "per_summary": "~$0,002",
             "note": "γρήγορο και φθηνό"},
            {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini", "price": "$0,75 / $4,50", "per_summary": "~$0,006",
             "note": "πιο ικανό, λίγο ακριβότερο"},
        ],
    },
}
DEFAULT = "anthropic"
# Μοντέλο του Claude για τα scripts (π.χ. αξιολόγηση με το κλειδί του .env).
SCRIPT_MODEL = os.environ.get("LLM_MODEL", INFO["anthropic"]["models"][0]["id"])


def model_ids(provider: str) -> list[str]:
    return [m["id"] for m in INFO[provider]["models"]] if provider in INFO else []


def default_model(provider: str) -> str:
    return INFO[provider]["models"][0]["id"]


def public_list() -> list[dict]:
    """Ό,τι χρειάζεται η σελίδα (χωρίς regex)."""
    return [{"id": pid, "label": i["label"], "company": i["company"], "hint": i["hint"], "console": i["console"],
             "models": i["models"]} for pid, i in INFO.items()]


def detect(key: str) -> str | None:
    """Ο πάροχος από το πρόθεμα του κλειδιού."""
    for pid, i in INFO.items():
        if re.match(i["pattern"], key):
            return pid
    return None


def looks_like_key(provider: str, key: str) -> bool:
    return provider in INFO and bool(re.match(INFO[provider]["pattern"], key))


def make(provider: str, key: str, model: str | None = None) -> "Provider":
    model = model or default_model(provider)
    if model not in model_ids(provider):
        raise ValueError(f"Μη διαθέσιμο μοντέλο: {model}")
    return {"anthropic": Anthropic, "openai": OpenAI}[provider](key=key, model=model)


def parse_bullets(text: str) -> list:
    """Οι κουκκίδες από την απάντηση JSON· ανεκτικό σε κείμενο γύρω από το JSON (π.χ. ```json … ```)."""
    try:
        return json.loads(text)["bullets"]
    except (ValueError, KeyError, TypeError):
        pass
    m = re.search(r"\{.*\}", text or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))["bullets"]
        except (ValueError, KeyError, TypeError):
            pass
    raise SummaryUnavailable("Η σύνοψη δεν είναι διαθέσιμη αυτή τη στιγμή.")


class Provider:
    id = ""

    def __init__(self, model: str):
        self.model = model

    @property
    def label(self) -> str:
        return INFO[self.id]["label"]

    def complete(self, system: str, user: str, schema: dict) -> str:
        raise NotImplementedError

    def validate(self) -> str | None:
        raise NotImplementedError


# ---------------------------------------------------------------- Claude (Anthropic SDK)

class Anthropic(Provider):
    id = "anthropic"

    def __init__(self, key: str | None = None, client=None, model: str | None = None):
        super().__init__(model or SCRIPT_MODEL)
        if client is None:
            import anthropic
            client = anthropic.Anthropic(api_key=key, timeout=60.0, max_retries=2)
        self.client = client

    def complete(self, system: str, user: str, schema: dict) -> str:
        import anthropic

        kwargs = dict(
            model=self.model,
            max_tokens=4000,  # περιθώριο για τη σκέψη του μοντέλου· η απάντηση είναι μικρή
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        )
        # Server-side fallback σε περίπτωση άρνησης· δεν υπάρχει για τα μοντέλα Haiku.
        if "haiku" not in self.model:
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            response = self.client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            log.warning("anthropic: το κλειδί API απορρίφθηκε")
            raise KeyRejected("Το κλειδί δεν έγινε δεκτό από το Anthropic (ίσως ανακλήθηκε).") from e
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():  # 400 όταν ο λογαριασμός δεν έχει υπόλοιπο
                raise KeyRejected("Ο λογαριασμός σας στο Anthropic δεν έχει αρκετό υπόλοιπο.") from e
            log.warning("anthropic: σφάλμα %s", type(e).__name__)
            raise SummaryUnavailable("Η σύνοψη δεν είναι διαθέσιμη αυτή τη στιγμή.") from e
        except anthropic.RateLimitError as e:
            raise SummaryUnavailable("Η υπηρεσία συνόψεων είναι προσωρινά απασχολημένη.") from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            log.warning("anthropic: σφάλμα %s", type(e).__name__)
            raise SummaryUnavailable("Η σύνοψη δεν είναι διαθέσιμη αυτή τη στιγμή.") from e
        u = response.usage
        log.info("LLM: provider=anthropic model=%s in=%s out=%s stop=%s", response.model, u.input_tokens,
                 u.output_tokens, response.stop_reason)
        if response.stop_reason == "refusal":
            raise SummaryUnavailable("Δεν ήταν δυνατή η σύνοψη για αυτόν τον μήνα.")
        return next((b.text for b in response.content if b.type == "text"), "")

    def validate(self) -> str | None:
        import anthropic

        try:
            self.client.with_options(timeout=15.0, max_retries=1).models.retrieve(self.model)
        except anthropic.AuthenticationError:
            return "Το κλειδί δεν έγινε δεκτό από το Anthropic. Ελέγξτε ότι το αντιγράψατε ολόκληρο."
        except (anthropic.PermissionDeniedError, anthropic.NotFoundError):
            return f"Το κλειδί δεν έχει πρόσβαση στο μοντέλο {self.model}."
        except anthropic.RateLimitError:
            return "Ο λογαριασμός σας στο Anthropic έφτασε το όριο αιτημάτων. Δοκιμάστε σε λίγο."
        except (anthropic.APIStatusError, anthropic.APIConnectionError):
            return "Δεν ήταν δυνατός ο έλεγχος του κλειδιού αυτή τη στιγμή. Δοκιμάστε ξανά."
        return None


# ---------------------------------------------------------------- ChatGPT (OpenAI Chat Completions, HTTPS)

class OpenAI(Provider):
    id = "openai"
    BASE = "https://api.openai.com/v1"

    def __init__(self, key: str, model: str | None = None):
        super().__init__(model or default_model("openai"))
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 60.0) -> httpx2.Response:
        try:
            return httpx2.request(method, self.BASE + path, json=body, timeout=timeout, headers=self._headers)
        except httpx2.HTTPError as e:
            log.warning("openai: σφάλμα δικτύου %s", type(e).__name__)
            raise SummaryUnavailable("Δεν ήταν δυνατή η σύνδεση με το OpenAI. Δοκιμάστε ξανά.") from None

    def _raise_for(self, r: httpx2.Response) -> None:
        """Μεταφράζει τα σφάλματα HTTP· δεν εκθέτει ποτέ το σώμα της απάντησης."""
        if r.status_code < 400:
            return
        body = r.text.lower()
        log.warning("openai: HTTP %s", r.status_code)
        if r.status_code in (401, 403):
            raise KeyRejected("Το κλειδί δεν έγινε δεκτό από το OpenAI.")
        if "insufficient_quota" in body or "billing" in body:
            raise KeyRejected("Ο λογαριασμός σας στο OpenAI δεν έχει αρκετό υπόλοιπο ή όριο χρέωσης.")
        if r.status_code == 429:
            raise SummaryUnavailable("Το OpenAI είναι προσωρινά απασχολημένο ή φτάσατε το όριο αιτημάτων.")
        raise SummaryUnavailable("Η σύνοψη δεν είναι διαθέσιμη αυτή τη στιγμή.")

    def complete(self, system: str, user: str, schema: dict) -> str:
        r = self._request("POST", "/chat/completions", {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "summary", "schema": schema, "strict": True}},
            "max_completion_tokens": 2000,
        })
        self._raise_for(r)
        data = r.json()
        u = data.get("usage") or {}
        choice = (data.get("choices") or [{}])[0]
        log.info("LLM: provider=openai model=%s in=%s out=%s stop=%s", data.get("model"), u.get("prompt_tokens"),
                 u.get("completion_tokens"), choice.get("finish_reason"))
        msg = choice.get("message") or {}
        if msg.get("refusal"):
            raise SummaryUnavailable("Δεν ήταν δυνατή η σύνοψη για αυτόν τον μήνα.")
        return msg.get("content") or ""

    def validate(self) -> str | None:
        r = self._request("GET", f"/models/{self.model}", timeout=15.0)
        if r.status_code == 404:
            return f"Το κλειδί δεν έχει πρόσβαση στο μοντέλο {self.model}."
        try:
            self._raise_for(r)
        except SummaryUnavailable as e:
            return str(e)
        return None
