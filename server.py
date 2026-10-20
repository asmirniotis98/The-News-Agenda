"""Η Ατζέντα των Ειδήσεων — API (FastAPI) και στατική σελίδα.

Τοπικά:      python server.py               → http://127.0.0.1:8000
Παραγωγή:    uvicorn server:app --host 0.0.0.0 --port 7860 --proxy-headers --forwarded-allow-ips="*"
Τεκμηρίωση API: /docs
"""

import os
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src import peaks
from src.data_files import ensure_data
from src.search import NewsIndex, make_query

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

EXAMPLES = ["μνημόνιο", "πανδημία", "ακρίβεια", "μεταναστευτικό", "σεισμός", "Ουκρανία", "ενεργειακή κρίση"]
MAX_TERM = 60
ARTICLE_URL = re.compile(r"^https://(www\.)?amna\.gr/")

# Όρια αιτημάτων: (πλήθος, παράθυρο σε δευτερόλεπτα). Ανά επισκέπτη (IP) και συνολικά για όλους, ώστε ακόμη κι αν
# κάποιος παρακάμψει το όριο ανά IP, ο server να μην υπερφορτώνεται.
RATE_LIMITS = {"search": (60, 60), "articles": (120, 60), "search_all": (600, 60), "articles_all": (1200, 60)}

# Πόσοι αξιόπιστοι proxies βρίσκονται μπροστά από τον server (στο Hugging Face: TRUSTED_PROXY_HOPS στο Dockerfile).
# Κάθε proxy προσθέτει μια IP στο ΤΕΛΟΣ του X-Forwarded-For. Οι πρώτες τιμές μπορεί να τις έχει γράψει ο ίδιος ο
# επισκέπτης, άρα η πραγματική IP είναι η N-οστή από το τέλος — ποτέ η πρώτη. Τοπικά (0): η IP της σύνδεσης.
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))

# Επιτρέπεται μόνο το δικό μας origin, τα συγκεκριμένα CDN και η ενσωμάτωση στη σελίδα του Hugging Face.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://cdn.jsdelivr.net",
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
    yield
    idx.con.close()


app = FastAPI(
    title="Η Ατζέντα των Ειδήσεων — API",
    description="Πορεία ενός όρου στην ειδησεογραφία του ΑΠΕ-ΜΠΕ (2011–2026) και οι κορυφώσεις του.",
    version="0.4.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def security(request: Request, call_next):
    path = request.url.path
    bucket = "search" if path == "/api/search" else "articles" if path == "/api/articles" else None
    if bucket:
        wait = limiter.check(client_ip(request), bucket) or limiter.check("*", bucket + "_all")
        if wait is not None:
            return JSONResponse({"detail": "Πάρα πολλά αιτήματα. Δοκιμάστε ξανά σε λίγο."},
                                status_code=429, headers={"Retry-After": str(wait), **SECURITY_HEADERS})
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if path.startswith("/api/"):
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
def meta():
    idx = state["idx"]
    return {
        "categories": idx.categories,
        "category_since": {c: m.strftime("%Y-%m") for c, m in idx.category_since.items()},
        "months": [state["months"][0], state["months"][-1]],
        "examples": EXAMPLES,
        "methods": peaks.METHODS,
        "max_term": MAX_TERM,
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
    }


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
