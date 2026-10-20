"""Έλεγχοι πρόσβασης με κωδικό και συνόψεων (χωρίς πραγματικές κλήσεις στο LLM).

Εκτέλεση από τον φάκελο του project, ένα σενάριο κάθε φορά (οι ρυθμίσεις διαβάζονται κατά την εκκίνηση):
    python tests/scenarios.py gate | misconfigured | short | summarizer claude-opus-5-5 | summarizer claude-haiku-5-5
"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, ".")
scenario = sys.argv[1]
ok = lambda cond, label: print(("PASS " if cond else "FAIL ") + label) or cond  # noqa: E731

if scenario == "gate":
    os.environ["ACCESS_CODE"] = "test-code-1234567"  # local test value only
    os.environ["REQUIRE_ACCESS_CODE"] = "1"
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app, base_url="https://testserver", follow_redirects=False) as c:
        r = c.get("/"); ok(r.status_code == 303 and r.headers["location"].startswith("/login?next=%2F"), "page without cookie -> login")
        r = c.get("/?q=abc"); ok("next=%2F%3Fq%3Dabc" in r.headers["location"], "original search kept in next=")
        ok(c.get("/api/search", params={"q": "abc"}).status_code == 401, "API without cookie -> 401")
        ok(c.get("/api/summary", params={"q": "abc", "month": "2015-08"}).status_code == 401, "summary API without cookie -> 401")
        ok(c.get("/docs").status_code == 401 and c.get("/openapi.json").status_code == 401, "docs without cookie -> 401")
        ok(c.get("/static/app.js").status_code == 303, "app.js without cookie -> login")
        ok(all(c.get(p).status_code == 200 for p in ["/login", "/static/app.css", "/static/login.js", "/healthz", "/robots.txt"]), "login page and its assets public")
        ok(c.get("/login").headers.get("x-robots-tag") == "noindex, nofollow", "noindex header")
        ok("Disallow: /" in c.get("/robots.txt").text, "robots.txt disallows all")
        r = c.post("/login", content="code=wrong&next=%2F", headers={"content-type": "application/x-www-form-urlencoded"})
        ok(r.status_code == 303 and "error=1" in r.headers["location"] and "set-cookie" not in r.headers, "wrong code -> error, no cookie")
        r = c.post("/login", content="code=test-code-1234567&next=%2F%2Fevil.example", headers={"content-type": "application/x-www-form-urlencoded"})
        ok(r.status_code == 303 and r.headers["location"] == "/", "open redirect //evil blocked")
        sc = r.headers.get("set-cookie", "")
        ok("HttpOnly" in sc and "Secure" in sc and "SameSite=lax" in sc.replace("samesite", "SameSite"), f"cookie flags: {sc.split(';',1)[1].strip()[:80]}")
        ok(c.get("/api/search", params={"q": "abc"}).status_code == 200, "with cookie -> API works")
        ok(c.get("/").status_code == 200 and c.get("/docs").status_code == 200, "with cookie -> page and docs work")
        m = c.get("/api/meta").json(); ok(m["gated"] is True and m["summaries"] is False, "meta: gated, summaries off (no key)")
        r = c.get("/api/summary", params={"q": "μνημόνιο", "month": "2015-08"}); ok(r.status_code == 403, f"summary with LLM switch off -> 403 ({r.json()['detail']})")
        ok(c.post("/api/llm/enable", json={}).status_code == 503, "no API key -> switch cannot be enabled (503)")
        c.cookies.set("na_session", "9999999999.deadbeef"); ok(c.get("/api/search", params={"q": "abc"}).status_code == 401, "forged cookie rejected")
        c.cookies.clear()
        codes = [c.post("/login", content="code=nope", headers={"content-type": "application/x-www-form-urlencoded"}).headers["location"] for _ in range(12)]
        ok("error=wait" in codes[-1], f"brute force: blocked after {sum('error=1' in x for x in codes)} attempts")

elif scenario == "misconfigured":
    os.environ.pop("ACCESS_CODE", None)
    os.environ["REQUIRE_ACCESS_CODE"] = "1"
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app, follow_redirects=False) as c:
        ok(c.get("/").status_code == 503 and c.get("/api/search", params={"q": "a"}).status_code == 503, "no code configured -> app closed (503)")
        ok(c.get("/healthz").status_code == 200, "health check still answers")
    os.environ["ACCESS_CODE"] = "short"

elif scenario == "short":
    os.environ["ACCESS_CODE"] = "short"
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app, follow_redirects=False) as c:
        ok(c.get("/").status_code == 503, "too-short code -> app closed (503)")

elif scenario == "summarizer":
    os.environ["SUMMARY_DB"] = os.path.join(tempfile.mkdtemp(), "s.sqlite")
    os.environ["DAILY_SUMMARY_LIMIT"] = "2"
    model = sys.argv[2]
    os.environ["LLM_MODEL"] = model
    from src.search import NewsIndex, make_query
    from src import summarize as S

    calls = []

    class Fake:
        def __init__(self, reply, stop="end_turn"):
            self.reply, self.stop = reply, stop
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(stop_reason=self.stop, model=kw["model"],
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="text", text=self.reply)])

    idx = NewsIndex()
    s = S.Summarizer(idx, client=Fake(json.dumps({"bullets": ["• Πρώτο", "Δεύτερο", "Τρίτο", "Τέταρτο", "Πέμπτο"]})))
    q = make_query("μνημόνιο")
    a = s.summary(q, "2015-08", "text", [])
    ok(len(a["bullets"]) == 4 and a["bullets"][0] == "Πρώτο" and not a["cached"], "bullets cleaned and capped at 4")
    kw = calls[0]
    ok("<titles>" in kw["messages"][0]["content"] and kw["messages"][0]["content"].count("\n- ") == a["n_titles"], f"prompt has {a['n_titles']} titles inside <titles>")
    ok(a["n_titles"] <= S.MAX_TITLES and a["n_articles"] == 149, f"titles capped ({a['n_titles']}), month has {a['n_articles']} articles")
    ok("temperature" not in kw and kw["output_config"]["format"]["type"] == "json_schema", "no temperature; JSON schema output")
    ok(("fallbacks" in kw) == ("haiku" not in model), f"fallbacks {'on' if 'fallbacks' in kw else 'off'} for {model}")
    b = s.summary(q, "2015-08", "text", [])
    ok(b["cached"] and len(calls) == 1, "second request served from cache (no LLM call)")
    s.summary(q, "2015-02", "text", [])
    try:
        s.summary(q, "2014-10", "text", []); ok(False, "budget")
    except S.BudgetExceeded:
        ok(s.usage_today() == 2, "3rd new summary blocked by daily limit=2")
    ok(s.summary(q, "2015-08", "text", [])["cached"], "cached summaries still served after budget is spent")
    os.environ["DAILY_SUMMARY_LIMIT"] = "100"
    S.DAILY_LIMIT = 100
    s2 = S.Summarizer(idx, client=Fake("", stop="refusal"), db=S.Path(os.environ["SUMMARY_DB"]))
    try:
        s2.summary(q, "2013-03", "text", []); ok(False, "refusal")
    except S.SummaryUnavailable as e:
        ok(True, f"refusal -> friendly error ({e})")

elif scenario in ("llm_pass", "llm_fail", "llm_required_unconfigured"):
    # Cloudflare's published Turnstile test keys: secret 1x... always passes, 2x... always fails.
    os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
    os.environ["SUMMARY_DB"] = os.path.join(tempfile.mkdtemp(), "s.sqlite")
    if scenario == "llm_required_unconfigured":
        os.environ["REQUIRE_CAPTCHA"] = "1"
    else:
        os.environ["TURNSTILE_SITE_KEY"] = "1x00000000000000000000AA"
        os.environ["TURNSTILE_SECRET_KEY"] = ("1x0000000000000000000000000000000AA" if scenario == "llm_pass"
                                              else "2x0000000000000000000000000000000AA")
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app, base_url="https://testserver") as c:
        fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
            stop_reason="end_turn", model=kw["model"], usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            content=[SimpleNamespace(type="text", text=json.dumps({"bullets": ["Δοκιμή"]}))]))))
        server.state["summarizer"].client = fake  # never call the real API in tests
        m = c.get("/api/meta").json()["llm"]
        q = {"q": "μνημόνιο", "month": "2015-08"}
        if scenario == "llm_required_unconfigured":
            ok(m["available"] is False, "captcha required but not configured -> switch unavailable")
            ok(c.post("/api/llm/enable", json={"token": "x"}).status_code == 503, "enable refused (fail closed)")
            ok(c.get("/api/summary", params=q).status_code == 403, "summary refused")
        else:
            ok(m["available"] and not m["enabled"] and bool(m["captcha_site_key"]), "switch available, off by default, site key sent")
            ok(c.get("/api/summary", params=q).status_code == 403, "summary before enabling -> 403")
            ok(c.post("/api/llm/enable", json={}).status_code == 403, "enable without captcha token -> 403")
            r = c.post("/api/llm/enable", json={"token": "XXXX.DUMMY.TOKEN.XXXX"})
            if scenario == "llm_fail":
                ok(r.status_code == 403 and "na_llm" not in c.cookies, "Cloudflare says fail -> not enabled")
            else:
                ok(r.status_code == 200 and "na_llm" in c.cookies, "Cloudflare says pass -> enabled (cookie set)")
                sc = r.headers.get("set-cookie", "")
                ok("HttpOnly" in sc and "Secure" in sc, "LLM cookie HttpOnly + Secure")
                ok(c.get("/api/meta").json()["llm"]["enabled"], "meta reports enabled")
                r = c.get("/api/summary", params=q)
                ok(r.status_code == 200 and r.json()["bullets"] == ["Δοκιμή"], "summary works after enabling")
                c.post("/api/llm/disable")
                ok(c.get("/api/summary", params=q).status_code == 403, "after disabling -> 403 again (next enable needs captcha)")
                c.cookies.set("na_llm", "9999999999.forged")
                ok(c.get("/api/summary", params=q).status_code == 403, "forged LLM cookie rejected")
