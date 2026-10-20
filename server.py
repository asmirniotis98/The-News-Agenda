"""Η Ατζέντα των Ειδήσεων — API (FastAPI) και στατική σελίδα.

Τοπικά:      python server.py               → http://127.0.0.1:8000
Παραγωγή:    βλ. Dockerfile (Hugging Face Spaces)
Τεκμηρίωση API: /docs
"""

import json
import logging
import os
import re
from urllib.parse import parse_qs, quote
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from src.env import load_env

load_env()  # πρώτα το .env (τοπικά), ώστε τα παρακάτω modules να δουν τις ρυθμίσεις του

from src import access, llm_gate, peaks  # noqa: E402
from src.data_files import ensure_data
from src.search import NewsIndex, make_query
from src.summarize import BudgetExceeded, Summarizer, SummaryUnavailable

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

EXAMPLES = ["μνημόνιο", "πανδημία", "ακρίβεια", "μεταναστευτικό", "σεισμός", "Ουκρανία", "ενεργειακή κρίση"]
MAX_TERM = 60
ARTICLE_URL = re.compile(r"^https://(www\.)?amna\.gr/")

# Άδεια δεδομένων CC BY-NC-ND 4.0: αναφορά δημιουργού, άδειας και πηγής σε κάθε απάντηση με δεδομένα.
SOURCE = {
    "creator": "Αθηναϊκό-Μακεδονικό Πρακτορείο Ειδήσεων (ΑΠΕ-ΜΠΕ)",
    "copyright": "© ΑΠΕ-ΜΠΕ — όλα τα δικαιώματα του περιεχομένου ανήκουν στο ΑΠΕ-ΜΠΕ",
    "dataset": "https://huggingface.co/datasets/glossAPI/amna-press",
    "license": "CC BY-NC-ND 4.0",
    "license_url": "https://creativecommons.org/licenses/by-nc-nd/4.0/",
    "notes": "Τίτλοι αυτούσιοι με σύνδεσμο στο πρωτότυπο· αριθμοί/κορυφές = στατιστικά της εφαρμογής. Μόνο μη εμπορική χρήση.",
}

# Όρια αιτημάτων: (πλήθος, παράθυρο σε δευτερόλεπτα). Ανά επισκέπτη (IP) και συνολικά για όλους, ώστε ακόμη κι αν
# κάποιος παρακάμψει το όριο ανά IP, ο server να μην υπερφορτώνεται.
RATE_LIMITS = {
    "search": (60, 60), "search_all": (600, 60),
    "articles": (120, 60), "articles_all": (1200, 60),
    "summary": (40, 600), "summary_all": (400, 600),   # οι συνόψεις κοστίζουν· βλ. και το ημερήσιο όριο
    "login": (10, 900), "login_all": (100, 900),       # προσπάθειες εισόδου: προστασία από δοκιμές κωδικών
    "llm_toggle": (20, 600), "llm_toggle_all": (300, 600),
}
BUCKETS = {"/api/search": "search", "/api/articles": "articles", "/api/summary": "summary",
           "/api/llm/enable": "llm_toggle"}
DOC_PATHS = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}

# Πόσοι αξιόπιστοι proxies βρίσκονται μπροστά από τον server (στο Hugging Face: TRUSTED_PROXY_HOPS στο Dockerfile).
# Κάθε proxy προσθέτει μια IP στο ΤΕΛΟΣ του X-Forwarded-For. Οι πρώτες τιμές μπορεί να τις έχει γράψει ο ίδιος ο
# επισκέπτης, άρα η πραγματική IP είναι η N-οστή από το τέλος — ποτέ η πρώτη. Τοπικά (0): η IP της σύνδεσης.
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))

# Επιτρέπεται μόνο το δικό μας origin, τα συγκεκριμένα CDN και η ενσωμάτωση στη σελίδα του Hugging Face.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net https://challenges.cloudflare.com",  # Turnstile («δεν είμαι ρομπότ»)
    "frame-src https://challenges.cloudflare.com",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",  # το Plotly προσθέτει inline styles
    "font-src https://cdn.jsdelivr.net",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'self' https://huggingface.co",
])
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Strict-Transport-Security": "max-age=31536000",  # οι browsers αγνοούν την οδηγία σε σύνδεση χωρίς HTTPS (τοπικά)
    "X-Robots-Tag": "noindex, nofollow",  # η εφαρμογή δεν εμφανίζεται σε μηχανές αναζήτησης
}


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    if not TRUSTED_PROXY_HOPS:
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    # Λιγότερες τιμές απ' όσους proxies: το αίτημα δεν πέρασε από αυτούς — μετράμε τη σύνδεση.
    return hops[-TRUSTED_PROXY_HOPS] if len(hops) >= TRUSTED_PROXY_HOPS else peer


class RateLimiter:
    """Κυλιόμενο παράθυρο ανά (IP, κατηγορία). Μόνο στη μνήμη — οι IP δεν αποθηκεύονται πουθενά."""

    def __init__(self, limits: dict[str, tuple[int, int]]):
        self.limits = limits
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, ip: str, bucket: str) -> int | None:
        """None αν επιτρέπεται, αλλιώς δευτερόλεπτα αναμονής."""
        limit, window = self.limits[bucket]
        now = time.monotonic()
        with self.lock:
            q = self.hits[(ip, bucket)]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return int(q[0] + window - now) + 1
            q.append(now)
            if len(self.hits) > 50_000:  # προστασία μνήμης: πετάμε όσους δεν έχουν πρόσφατα αιτήματα
                for key in [k for k, v in self.hits.items() if not v]:
                    del self.hits[key]
            return None


limiter = RateLimiter(RATE_LIMITS)
state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_data()
    idx = NewsIndex()
    state["idx"] = idx
    state["months"] = [pd.Timestamp(m).strftime("%Y-%m") for m in idx.months.month]
    state["summarizer"] = Summarizer(idx)
    yield
    idx.con.close()


app = FastAPI(
    title="Η Ατζέντα των Ειδήσεων — API",
    description=("Πορεία ενός όρου στην ειδησεογραφία του ΑΠΕ-ΜΠΕ (2011–2026) και οι κορυφώσεις του.\n\n"
                 "**Δεδομένα:** © ΑΠΕ-ΜΠΕ, μέσω [glossAPI/amna-press](https://huggingface.co/datasets/glossAPI/amna-press), "
                 "άδεια [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/) — μόνο μη εμπορική χρήση, "
                 "με αναφορά της πηγής. Κάθε απάντηση περιέχει το πεδίο `source`."),
    version="0.5.0",
    lifespan=lifespan,
)


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


def too_many(wait: int) -> JSONResponse:
    return JSONResponse({"detail": "Πάρα πολλά αιτήματα. Δοκιμάστε ξανά σε λίγο."},
                        status_code=429, headers={"Retry-After": str(wait), **SECURITY_HEADERS})


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path

    # 1. Πρόσβαση με κωδικό
    if access.misconfigured() and path != "/healthz":
        return PlainTextResponse("Η εφαρμογή δεν έχει ρυθμιστεί (λείπει ο κωδικός πρόσβασης).",
                                 status_code=503, headers=SECURITY_HEADERS)
    if (access.enabled() and path not in access.PUBLIC_PATHS
            and not access.token_valid(request.cookies.get(access.COOKIE))):
        if path.startswith("/api/") or path in DOC_PATHS:
            return JSONResponse({"detail": "Απαιτείται κωδικός πρόσβασης."}, status_code=401, headers=SECURITY_HEADERS)
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303, headers=SECURITY_HEADERS)

    # 2. Όρια αιτημάτων (ανά επισκέπτη και συνολικά)
    bucket = BUCKETS.get(path) or ("login" if path == "/login" and request.method == "POST" else None)
    if bucket:
        wait = limiter.check(client_ip(request), bucket) or limiter.check("*", bucket + "_all")
        if wait is not None:
            if bucket == "login":
                return RedirectResponse("/login?error=wait", status_code=303, headers=SECURITY_HEADERS)
            return too_many(wait)

    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if path.startswith("/api/") or path.startswith("/login"):
        response.headers["Cache-Control"] = "no-store"
    elif path == "/" or path.startswith("/static/"):
        # Ο browser ελέγχει κάθε φορά αν υπάρχει νεότερη έκδοση (ETag), ώστε μετά από ενημέρωση να μη δείχνει παλιά σελίδα.
        response.headers["Cache-Control"] = "no-cache"
    return response


# ---------------------------------------------------------------- έλεγχος εισόδων

def clean_term(q: str) -> str:
    q = " ".join(q.split())
    if not q or len(q) > MAX_TERM or any(ord(c) < 32 for c in q):
        raise HTTPException(422, f"Ο όρος πρέπει να έχει 1–{MAX_TERM} χαρακτήρες.")
    return q


def clean_categories(cats: list[str] | None) -> list[str]:
    cats = cats or []
    unknown = set(cats) - set(state["idx"].categories)
    if unknown:
        raise HTTPException(422, f"Άγνωστη κατηγορία: {', '.join(sorted(unknown))}")
    return cats


Term = Annotated[str, Query(min_length=1, max_length=MAX_TERM, description="Λέξη ή φράση, π.χ. «μνημόνιο»")]
Field = Annotated[Literal["text", "title"], Query(description="Αναζήτηση σε τίτλο + κείμενο ή μόνο σε τίτλους")]
Categories = Annotated[list[str] | None, Query(alias="cat", description="Κατηγορίες (επαναλαμβανόμενη παράμετρος)")]
AllForms = Annotated[bool, Query(description="Όλοι οι κλιτικοί τύποι της λέξης")]


# ---------------------------------------------------------------- API

@app.get("/api/meta", summary="Ρυθμίσεις για τη σελίδα")
def meta(request: Request):
    idx = state["idx"]
    return {
        "categories": idx.categories,
        "category_since": {c: m.strftime("%Y-%m") for c, m in idx.category_since.items()},
        "months": [state["months"][0], state["months"][-1]],
        "examples": EXAMPLES,
        "methods": peaks.METHODS,
        "max_term": MAX_TERM,
        "summaries": state["summarizer"].enabled,
        "llm": {
            "available": state["summarizer"].enabled and llm_gate.available(),
            "enabled": llm_gate.token_valid(request.cookies.get(llm_gate.COOKIE)),
            "captcha_site_key": llm_gate.SITE_KEY if llm_gate.captcha_configured() else None,
        },
        "gated": access.enabled(),
        "source": SOURCE,
    }


@app.get("/api/search", summary="Άρθρα ανά μήνα και κορυφές για έναν όρο")
def search(
    q: Term,
    all_forms: AllForms = True,
    field: Field = "text",
    cat: Categories = None,
    year_from: Annotated[int, Query(ge=2000, le=2100)] = 2011,
    year_to: Annotated[int, Query(ge=2000, le=2100)] = 2026,
    metric: Annotated[Literal["share", "hits"], Query(description="Κορυφές με βάση το ποσοστό ή το πλήθος")] = "share",
    method: Annotated[Literal["prominence", "zscore"], Query()] = "prominence",
    n: Annotated[int, Query(ge=1, le=10)] = 6,
):
    term, cats = clean_term(q), clean_categories(cat)
    query = make_query(term, all_forms)
    df = state["idx"].monthly(query, field, cats)
    df = df[df["month"].dt.year.between(year_from, year_to)].reset_index(drop=True)
    pk = peaks.detect(df, method, n, column=metric)
    top = df.loc[df[metric].idxmax()] if df["hits"].sum() else None
    return {
        "term": term,
        "label": query.label,
        "total_hits": int(df["hits"].sum()),
        "max_month": top["month"].strftime("%Y-%m") if top is not None else None,
        "max_share": round(float(top["share"]), 2) if top is not None else None,
        "series": {
            "month": df["month"].dt.strftime("%Y-%m").tolist(),
            "hits": df["hits"].astype(int).tolist(),
            "total": df["total"].astype(int).tolist(),
            "share": df["share"].round(3).tolist(),
        },
        "peaks": [
            {"month": r.month.strftime("%Y-%m"), "rank": int(r.rank), "hits": int(r.hits),
             "total": int(r.total), "share": round(float(r.share), 2)}
            for r in pk.itertuples()
        ],
        "source": SOURCE,
    }


@app.get("/api/articles", summary="Τα άρθρα ενός μήνα που περιέχουν τον όρο")
def articles(
    q: Term,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$", description="Μήνας ως ΕΕΕΕ-ΜΜ")],
    all_forms: AllForms = True,
    field: Field = "text",
    cat: Categories = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0, le=10_000)] = 0,
):
    term, cats = clean_term(q), clean_categories(cat)
    if month not in state["months"]:
        raise HTTPException(422, "Άγνωστος μήνας.")
    df = state["idx"].articles(make_query(term, all_forms), month + "-01", field, cats)
    page = df.iloc[offset:offset + limit]
    return {
        "total": len(df),
        "items": [
            {"date": r.created.strftime("%Y-%m-%d"), "category": r.category, "title": r.title,
             "url": r.url if ARTICLE_URL.match(r.url or "") else None, "in_title": bool(r.in_title)}
            for r in page.itertuples()
        ],
        "source": SOURCE,
    }


@app.get("/api/summary", summary="Σύνοψη «τι συνέβη» σε έναν μήνα (LLM, από τους τίτλους των άρθρων)")
def summary(
    request: Request,
    q: Term,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$", description="Μήνας ως ΕΕΕΕ-ΜΜ")],
    all_forms: AllForms = True,
    field: Field = "text",
    cat: Categories = None,
):
    if not llm_gate.token_valid(request.cookies.get(llm_gate.COOKIE)):
        raise HTTPException(403, "Οι αυτόματες συνόψεις είναι απενεργοποιημένες. Ενεργοποιήστε τις από τον διακόπτη.")
    term, cats = clean_term(q), clean_categories(cat)
    if month not in state["months"]:
        raise HTTPException(422, "Άγνωστος μήνας.")
    try:
        result = state["summarizer"].summary(make_query(term, all_forms), month, field, cats)
    except BudgetExceeded as e:
        raise HTTPException(429, f"{e} Εμφανίζονται μόνο όσες έχουν ήδη δημιουργηθεί· δοκιμάστε ξανά αύριο.") from e
    except SummaryUnavailable as e:
        raise HTTPException(503, str(e)) from e
    return {
        **result,
        "notice": "Αυτόματη σύνοψη από γλωσσικό μοντέλο (LLM), μόνο με βάση τους τίτλους των άρθρων. "
                  "Μπορεί να περιέχει ανακρίβειες — ελέγξτε τους τίτλους και τα πρωτότυπα άρθρα.",
        "source": SOURCE,
    }


@app.post("/api/llm/enable", summary="Ενεργοποίηση συνόψεων (απαιτεί Turnstile token)")
async def llm_enable(request: Request):
    if not (state["summarizer"].enabled and llm_gate.available()):
        raise HTTPException(503, "Οι αυτόματες συνόψεις δεν είναι διαθέσιμες.")
    body = await request.body()
    if len(body) > 4096:
        raise HTTPException(413, "Πολύ μεγάλο αίτημα.")
    try:
        token = str(json.loads(body or b"{}").get("token") or "")
    except (ValueError, AttributeError):
        raise HTTPException(422, "Μη έγκυρο αίτημα.") from None
    # Η επαλήθευση καλεί το Cloudflare (έως ~30 s): σε ξεχωριστό νήμα, ώστε να μην παγώνει τον server για τους άλλους.
    if not await run_in_threadpool(llm_gate.verify, token, client_ip(request)):
        raise HTTPException(403, "Η επιβεβαίωση «δεν είμαι ρομπότ» απέτυχε. Δοκιμάστε ξανά.")
    response = JSONResponse({"enabled": True})
    response.set_cookie(llm_gate.COOKIE, llm_gate.new_token(), max_age=llm_gate.SECONDS,
                        httponly=True, samesite="lax", secure=is_https(request), path="/")
    return response


@app.post("/api/llm/disable", summary="Απενεργοποίηση συνόψεων")
def llm_disable():
    response = JSONResponse({"enabled": False})
    response.delete_cookie(llm_gate.COOKIE, path="/")
    return response


# ---------------------------------------------------------------- είσοδος με κωδικό

@app.get("/login", include_in_schema=False)
def login_page():
    if not access.enabled():
        return RedirectResponse("/", status_code=303)
    return FileResponse(WEB / "login.html")


@app.post("/login", include_in_schema=False)
async def login(request: Request):
    body = await request.body()
    if len(body) > 2048:
        raise HTTPException(413, "Πολύ μεγάλο αίτημα.")
    form = parse_qs(body.decode("utf-8", "replace"))
    code = (form.get("code") or [""])[0].strip()
    target = access.safe_next((form.get("next") or ["/"])[0])
    if not access.enabled() or not access.code_matches(code):
        return RedirectResponse(f"/login?error=1&next={quote(target, safe='')}", status_code=303)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(access.COOKIE, access.new_token(), max_age=access.SESSION_SECONDS,
                        httponly=True, samesite="lax", secure=is_https(request), path="/")
    return response


@app.get("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login" if access.enabled() else "/", status_code=303)
    response.delete_cookie(access.COOKIE, path="/")
    return response


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": "idx" in state}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


app.mount("/static", StaticFiles(directory=WEB), name="static")


if __name__ == "__main__":
    import uvicorn

    # Τοπικά ακούμε μόνο στον δικό μας υπολογιστή (127.0.0.1), όχι στο τοπικό δίκτυο.
    uvicorn.run("server:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8000)))
