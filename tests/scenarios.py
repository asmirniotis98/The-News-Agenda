"""Έλεγχοι πρόσβασης με κωδικό και συνόψεων (χωρίς πραγματικές κλήσεις στο LLM).

Εκτέλεση από τον φάκελο του project, ένα σενάριο κάθε φορά (οι ρυθμίσεις διαβάζονται κατά την εκκίνηση):
    python tests/scenarios.py gate | misconfigured | short | summarizer claude-opus-5-5 | summarizer claude-haiku-5-5
                              | llm_checkbox | byok
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
        m = c.get("/api/meta").json(); ok(m["gated"] is True and m["byok"]["active"] is False, "meta: gated, no visitor key")
        r = c.get("/api/summary", params={"q": "μνημόνιο", "month": "2015-08"}); ok(r.status_code == 403, f"summary with LLM switch off -> 403 ({r.json()['detail']})")
        ok(c.post("/api/llm/enable", json={}).status_code == 403, "switch needs the robot check (403)")
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
    os.environ["CAPTCHA_MODE"] = "turnstile"
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

elif scenario == "llm_checkbox":
    # Our own "I'm not a robot" box (default mode), as on the Space.
    import time as _t
    os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
    os.environ["SUMMARY_DB"] = os.path.join(tempfile.mkdtemp(), "s.sqlite")
    os.environ["REQUIRE_CAPTCHA"] = "1"
    from fastapi.testclient import TestClient
    import server
    from src import llm_gate
    with TestClient(server.app, base_url="https://testserver") as c:
        server.state["summarizer"].client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                               usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                               content=[SimpleNamespace(type="text", text=json.dumps({"bullets": ["Δοκιμή"]}))]))))
        m = c.get("/api/meta").json()["llm"]
        ok(m["available"] and m["captcha"] == "checkbox" and not m["enabled"], "checkbox mode available, off by default")
        q = {"q": "μνημόνιο", "month": "2015-08"}
        en = lambda body: c.post("/api/llm/enable", json=body).status_code  # noqa: E731
        ch = c.get("/api/llm/challenge").json()["challenge"]
        ok(en({"challenge": ch, "confirm": True, "website": ""}) == 403, "too fast (< 1.5 s) -> rejected")
        ch = c.get("/api/llm/challenge").json()["challenge"]; _t.sleep(1.7)
        ok(en({"challenge": ch, "confirm": False, "website": ""}) == 403, "box not ticked -> rejected")
        ok(en({"challenge": ch, "confirm": True, "website": "http://spam"}) == 403, "honeypot filled -> rejected")
        ok(en({"confirm": True}) == 403, "no challenge -> rejected")
        ok(en({"challenge": ch[:-4] + "abcd", "confirm": True, "website": ""}) == 403, "tampered challenge -> rejected")
        old = llm_gate.new_challenge(now=_t.time() - 400)
        ok(en({"challenge": old, "confirm": True, "website": ""}) == 403, "expired challenge (> 5 min) -> rejected")
        ok(c.post("/api/llm/enable", content=b"[1,2]", headers={"content-type": "application/json"}).status_code == 422, "non-object JSON -> 422")
        ok(c.get("/api/summary", params=q).status_code == 403, "still no summaries")
        ok(en({"challenge": ch, "confirm": True, "website": ""}) == 200 and "na_llm" in c.cookies, "ticked, human speed -> enabled")
        ok(c.get("/api/summary", params=q).json()["bullets"] == ["Δοκιμή"], "summaries work")
        c.post("/api/llm/disable"); c.cookies.clear()
        ok(en({"challenge": ch, "confirm": True, "website": ""}) == 403, "same challenge reused -> rejected")
        ok(c.get("/api/summary", params=q).status_code == 403, "after disabling -> 403 (new box needed)")

elif scenario == "byok":
    # Bring your own key: the visitor's key lives only in server memory, 30 min, bound to the visitor's IP.
    import io
    import logging
    import time as _t

    import anthropic
    import httpx

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-owner-key-must-never-be-used-000000"  # local test value only
    os.environ["SUMMARY_DB"] = os.path.join(tempfile.mkdtemp(), "s.sqlite")
    os.environ["REQUIRE_CAPTCHA"] = "1"
    os.environ["TRUSTED_PROXY_HOPS"] = "1"
    os.environ["BYOK_SESSION_LIMIT"] = "3"
    GOOD = "sk-ant-test-good-" + "a" * 30   # fake test keys
    BAD = "sk-ant-test-bad-" + "b" * 30
    REVOKED = "sk-ant-test-revoked-" + "c" * 30

    def api_error(cls, status):
        return cls("rejected", response=httpx.Response(status, request=httpx.Request("GET", "https://api.test")), body=None)

    class FakeClient:
        def __init__(self, key):
            self.key = key
            self.models = SimpleNamespace(retrieve=self.retrieve)
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

        def with_options(self, **_):
            return self

        def retrieve(self, model):
            if self.key == BAD:
                raise api_error(anthropic.AuthenticationError, 401)
            return SimpleNamespace(id=model)

        def create(self, **kw):
            if self.key == REVOKED:
                raise api_error(anthropic.AuthenticationError, 401)
            return SimpleNamespace(stop_reason="end_turn", model=kw["model"],
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="text", text=json.dumps({"bullets": ["Δοκιμή"]}))])

    logs = io.StringIO()
    logging.getLogger().addHandler(logging.StreamHandler(logs))
    logging.getLogger().setLevel(logging.DEBUG)  # όλα τα logs, και του Anthropic SDK / httpx
    from fastapi.testclient import TestClient
    import server
    from src import byok
    from src import providers as _prov
    byok.make_client = lambda key, provider="anthropic", model=None: _prov.Anthropic(client=FakeClient(key), model=model)  # never call the real API
    clock = [_t.time()]
    server.keys.clock = lambda: clock[0]

    with TestClient(server.app, base_url="https://testserver") as c:
        c.headers["x-forwarded-for"] = "203.0.113.5"
        ok(server.state["summarizer"].client is None, "owner's ANTHROPIC_API_KEY is ignored by the server")
        m = c.get("/api/meta").json()
        ok(m["llm"]["available"] and m["byok"] == {"active": False, "reason": None}, "meta: summaries available, no key")
        ok(c.post("/api/byok/key", json={"key": GOOD}).status_code == 403, "key before robot check -> 403")
        ch = c.get("/api/llm/challenge").json()["challenge"]; _t.sleep(1.7)
        ok(c.post("/api/llm/enable", json={"challenge": ch, "confirm": True, "website": ""}).status_code == 200, "robot check passed")
        q = {"q": "μνημόνιο", "month": "2015-08"}
        r = c.get("/api/summary", params=q)
        ok(r.status_code == 428 and r.json()["key_required"], "new summary without key -> 428 key_required")

        r = c.post("http://testserver/api/byok/key", json={"key": GOOD})
        ok(r.status_code == 403 and "HTTPS" in r.json()["detail"], "key over plain HTTP -> refused")
        r = c.post("/api/byok/key", json={"key": "not-a-key-" + "x" * 30})
        ok(r.status_code == 422 and "not-a-key" not in r.text, "malformed key -> 422, value not echoed")
        r = c.post("/api/byok/key", json={"key": 12345})
        ok(r.status_code == 422 and "12345" not in r.text, "non-string key -> 422, value not echoed")
        r = c.post("/api/byok/key", json={"key": BAD})
        ok(r.status_code == 400 and BAD not in r.text and "byok" not in str(r.headers.get("set-cookie")), "rejected key -> 400, no session")
        r = c.post("/api/byok/key", json={"key": GOOD})
        sc = r.headers.get("set-cookie", "")
        ok(r.status_code == 200 and r.json()["active"] and 1790 <= r.json()["expires_in"] <= 1800, "valid key -> active for 30 min")
        ok(GOOD not in r.text and GOOD not in sc, "key not in response or cookie")
        ok(all(f in sc for f in ("HttpOnly", "Secure", "Path=/api")) and "samesite=strict" in sc.lower()
           and "max-age" not in sc.lower(), "cookie: HttpOnly, Secure, SameSite=Strict, /api, session-only")

        a = c.get("/api/summary", params=q).json()
        ok(a["bullets"] == ["Δοκιμή"] and not a["cached"], "summary with visitor key")
        ok(c.get("/api/byok/status").json()["used"] == 1, "new summary counted on the session")
        ok(c.get("/api/summary", params=q).json()["cached"] and c.get("/api/byok/status").json()["used"] == 1, "cached summary not counted")
        c.get("/api/summary", params={**q, "month": "2015-02"}); c.get("/api/summary", params={**q, "month": "2015-03"})
        r = c.get("/api/summary", params={**q, "month": "2015-04"})
        ok(r.status_code == 429, f"session limit (3) reached -> 429 ({r.json()['detail'][:40]}…)")

        ok(c.post("/api/byok/renew").status_code == 409, "renew too early (> 5 min left) -> 409")
        clock[0] += 26 * 60
        r = c.post("/api/byok/renew")
        ok(r.status_code == 200 and r.json()["expires_in"] == 1800, "renew in last 5 min -> 30 min again")
        clock[0] += 31 * 60
        s = c.get("/api/byok/status").json()
        ok(not s["active"] and "30 λεπτά" in s["reason"], "after 30 min -> key gone")

        c.post("/api/byok/key", json={"key": GOOD})
        c.headers["x-forwarded-for"] = "198.51.100.7"
        r = c.get("/api/summary", params={**q, "month": "2014-10"})
        ok(r.status_code == 428 and "IP" in r.json()["detail"], "different IP -> 428, key dropped")
        c.headers["x-forwarded-for"] = "203.0.113.5"
        ok(not c.get("/api/byok/status").json()["active"], "back on the first IP -> still gone (must paste again)")
        ok(c.get("/api/summary", params=q).json()["cached"], "cached summaries still shown without a key")

        c.post("/api/byok/key", json={"key": REVOKED})
        r = c.get("/api/summary", params={**q, "month": "2013-03"})
        ok(r.status_code == 428 and not c.get("/api/byok/status").json()["active"], "key revoked at Anthropic -> 428, session dropped")

        c.post("/api/byok/key", json={"key": GOOD})
        c.post("/api/byok/forget")
        ok(not c.get("/api/byok/status").json()["active"] and not server.keys.sessions, "forget -> key removed from memory")
        c.post("/api/byok/key", json={"key": GOOD})
        c.post("/api/llm/disable")
        ok(not server.keys.sessions, "switching summaries off also removes the key")

    text = logs.getvalue()
    ok(all(k not in text for k in (GOOD, BAD, REVOKED)), f"no key in the logs ({len(text.splitlines())} log lines checked)")

elif scenario == "providers":
    # Claude / ChatGPT with model choice. OpenAI through mocked HTTP; Claude through a fake SDK client. No real calls.
    import logging
    import httpx2
    from src import providers as P
    os.environ["SUMMARY_DB"] = os.path.join(tempfile.mkdtemp(), "s.sqlite")
    os.environ["REQUIRE_CAPTCHA"] = "0"
    os.environ["CAPTCHA_MODE"] = "none"  # the robot box is tested in llm_checkbox; here only providers and models
    logs = []
    logging.getLogger().addHandler(type("H", (logging.Handler,), {"emit": lambda self, r: logs.append(r.getMessage())})())
    OAI, ANT = "sk-proj-" + "o" * 40, "sk-ant-" + "a" * 40
    OK = '{"bullets": ["Πρώτο", "Δεύτερο"]}'
    calls, state = [], {"status": 200, "body": None}

    def fake_request(method, url, json=None, timeout=None, headers=None):
        calls.append({"method": method, "url": url, "json": json, "headers": headers or {}})
        if state["body"] is not None:
            return httpx2.Response(state["status"], json=state["body"])
        if url.endswith("/chat/completions"):
            return httpx2.Response(200, json={"model": json["model"], "usage": {"prompt_tokens": 9, "completion_tokens": 3},
                                              "choices": [{"finish_reason": "stop", "message": {"content": OK}}]})
        return httpx2.Response(200, json={"id": url.rsplit("/", 1)[-1]})
    P.httpx2.request = fake_request

    def answer(status, body):
        state.update(status=status, body=body)

    ok(set(P.INFO) == {"anthropic", "openai"}, "only Claude and ChatGPT offered")
    ok(P.model_ids("anthropic") == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
       and P.model_ids("openai") == ["gpt-5.4-nano", "gpt-5.4-mini"], "model lists (cheapest first = default)")
    ok(P.detect(ANT) == "anthropic" and P.detect(OAI) == "openai" and P.detect("hello") is None, "provider detected from key prefix")
    ok(P.parse_bullets("```json\n" + OK + "\n```") == ["Πρώτο", "Δεύτερο"], "lenient JSON parsing (```json … ```)")
    try:
        P.make("openai", OAI, "gpt-5.4"); ok(False, "model allowlist")
    except ValueError:
        ok(True, "model outside the list refused (gpt-5.4)")

    calls.clear(); out = P.make("openai", OAI, "gpt-5.4-mini").complete("sys", "user", {"type": "object"})
    c = calls[-1]
    ok(out == OK and c["url"] == "https://api.openai.com/v1/chat/completions" and c["json"]["model"] == "gpt-5.4-mini",
       "OpenAI: fixed endpoint, chosen model sent")
    ok(c["headers"].get("Authorization") == "Bearer " + OAI and OAI not in c["url"], "OpenAI: key only in Authorization header")
    ok(c["json"]["response_format"]["type"] == "json_schema" and c["json"]["response_format"]["json_schema"]["strict"], "OpenAI: strict JSON schema")
    answer(429, {"error": {"code": "insufficient_quota"}})
    try:
        P.make("openai", OAI).complete("s", "u", {}); ok(False, "quota")
    except P.KeyRejected as e:
        ok(OAI not in str(e), f"OpenAI: no credit -> KeyRejected ({e})")
    answer(429, {"error": {"code": "rate_limit_exceeded"}})
    try:
        P.make("openai", OAI).complete("s", "u", {}); ok(False, "busy")
    except P.KeyRejected:
        ok(False, "rate limit is not a bad key")
    except P.SummaryUnavailable:
        ok(True, "OpenAI: rate limit -> temporary error (key kept)")
    answer(404, {})
    ok("δεν έχει πρόσβαση" in P.make("openai", OAI).validate() and calls[-1]["method"] == "GET", "validate: free GET on the model; 404 -> no access")
    answer(401, {"error": {"message": "Incorrect API key provided: sk-proj-oooo"}})
    msg = P.make("openai", OAI).validate()
    ok("δεν έγινε δεκτό" in msg and "sk-proj" not in msg, "validate: 401 -> rejected, provider text (with key) not echoed")
    answer(200, None); state["body"] = None

    from fastapi.testclient import TestClient
    import server
    from src import byok

    class FakeAnthropic:  # Claude SDK stand-in: models.retrieve + beta.messages.create
        def __init__(self):
            self.models = SimpleNamespace(retrieve=lambda model: calls.append({"method": "GET", "url": "anthropic:" + model, "json": None, "headers": {}}))
            self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        def with_options(self, **_):
            return self
        def create(self, **kw):
            calls.append({"method": "POST", "url": "anthropic:messages", "json": kw, "headers": {}})
            return SimpleNamespace(stop_reason="end_turn", model=kw["model"], usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                                   content=[SimpleNamespace(type="text", text=OK)])
    real_make = byok.make_client
    byok.make_client = lambda key, provider="anthropic", model=None: (
        P.Anthropic(client=FakeAnthropic(), model=model or P.default_model("anthropic")) if provider == "anthropic"
        else real_make(key, provider, model))

    with TestClient(server.app, base_url="https://testserver") as cl:
        prov = {p["id"]: p for p in cl.get("/api/meta").json()["llm"]["providers"]}
        ok(set(prov) == {"anthropic", "openai"} and [m["id"] for m in prov["openai"]["models"]] == P.model_ids("openai"),
           "meta: two providers with their models and prices")
        assert cl.post("/api/llm/enable", json={}).status_code == 200
        r = cl.post("/api/byok/key", json={"provider": "openai", "model": "gpt-5.4-mini", "key": OAI})
        st = r.json()
        ok(r.status_code == 200 and st.get("provider") == "openai" and st.get("model") == "gpt-5.4-mini" and OAI not in r.text,
           "server: ChatGPT + GPT-5.4 mini -> active, key not echoed")
        s = cl.get("/api/summary", params={"q": "μνημόνιο", "month": "2015-08"}).json()
        ok(s["bullets"] == ["Πρώτο", "Δεύτερο"] and s["model"] == "gpt-5.4-mini", "server: summary written by the chosen model")
        r = cl.post("/api/byok/key", json={"provider": "anthropic", "model": "claude-sonnet-5-5", "key": ANT})
        ok(r.json().get("model") == "claude-sonnet-5-5", "server: Claude + Sonnet 5.5 -> active")
        s = cl.get("/api/summary", params={"q": "μνημόνιο", "month": "2015-02"}).json()
        ok(s["model"] == "claude-sonnet-5-5" and calls[-1]["json"]["model"] == "claude-sonnet-5-5", "server: Claude call uses the chosen model")
        ok(cl.get("/api/summary", params={"q": "μνημόνιο", "month": "2015-08"}).json()["cached"], "cached summary (by GPT) shown to a Claude user")
        ok(cl.post("/api/byok/key", json={"provider": "openai", "model": "gpt-5.4", "key": OAI}).status_code == 422, "server: model outside list -> 422")
        ok(cl.post("/api/byok/key", json={"provider": "anthropic", "model": "gpt-5.4-nano", "key": ANT}).status_code == 422, "server: other provider's model -> 422")
        r = cl.post("/api/byok/key", json={"provider": "openai", "key": ANT})
        ok(r.status_code == 422 and "Ελέγξτε τον πάροχο" in r.json()["detail"] and ANT not in r.text, "server: Claude key under ChatGPT -> asks to check provider")
        ok(cl.post("/api/byok/key", json={"provider": "gemini", "key": "AIza" + "g" * 35}).status_code == 422, "server: Gemini not accepted")
        ok(all(c["url"].startswith(("https://api.openai.com/", "anthropic:")) for c in calls), "every outgoing request went to a fixed provider")
    joined = "\n".join(logs)
    ok(OAI not in joined and ANT not in joined, f"no key in the logs ({len(logs)} lines)")
