"""Regression tests for every defect the adversarial review confirmed."""
import asyncio, os, sqlite3, tempfile, time, types
os.environ.update(META_APP_SECRET="s", META_VERIFY_TOKEN="v", META_DRY_RUN="1",
                  ADMIN_TOKEN="adm", LEAD_WEBHOOK_URL="https://hook.example/l",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "fx.db"))
import app as A
from fastapi.testclient import TestClient

IG, FAN = "IG1", "FAN1"
def check(l, c):
    assert c, f"FAILED: {l}"; print(f"  ok  {l}")

def reset(**auto):
    with A.db() as con:
        A.ensure(con)
        for t in ("accounts","automations","contacts","sends","seen_events","followups",
                  "lead_deliveries","unresolved_asks"):
            con.execute(f"DELETE FROM {t}")
        con.execute("INSERT INTO accounts VALUES (?,?,?,?,?)",(IG,"me","t",int(time.time())+86400,int(time.time())))
        c = dict(ig_user_id=IG, keyword="GUIDE", private_reply="canned", ask_email="your email?",
                 email_thanks="thanks", public_reply=None, ai_brief=None,
                 followup_text=None, followup_after=None)
        c.update(auto)
        con.execute(f"INSERT INTO automations ({','.join(c)}) VALUES ({','.join('?'*len(c))})", tuple(c.values()))

def comment(text="GUIDE", cid="C1", frm=FAN, user="fan"):
    return {"object":"instagram","entry":[{"id":IG,"time":int(time.time()),"changes":[{"field":"comments",
        "value":{"id":cid,"text":text,"from":{"id":frm,"username":user}}}]}]}
def dm(text, mid="M1", sender=FAN):
    return {"object":"instagram","entry":[{"id":IG,"time":int(time.time()),"messaging":[{"sender":{"id":sender},
        "recipient":{"id":IG},"timestamp":int(time.time()*1000),"message":{"mid":mid,"text":text}}]}]}
def q(sql, *a):
    with A.db() as con: return con.execute(sql, a).fetchall()

print("1/4  transaction isolation")
check("db() runs in autocommit so no lock spans an await",
      sqlite3.connect(":memory:", isolation_level=None).isolation_level is None
      and "isolation_level=None" in open("app.py").read())
reset()
with A.db() as con:
    con.execute("INSERT INTO seen_events (key,at) VALUES ('x',1)")
check("a write is durable without an explicit commit", len(q("SELECT 1 FROM seen_events WHERE key='x'")) == 1)

reset()
# one malformed event must not discard a good one in the same batch
asyncio.run(A.process({"object":"instagram","entry":[
    {"id":IG,"time":"not-a-number","changes":[]},
    {"id":IG,"time":int(time.time()),"changes":[{"field":"comments","value":{"id":"CO","text":"GUIDE",
      "from":{"id":FAN,"username":"fan"}}}]}]}))
check("a malformed entry does not kill the rest of the batch", len(q("SELECT 1 FROM sends")) == 1)

print("\n2/4  retryable dedupe")
reset()
with A.db() as con:                                    # jam the hourly cap
    con.executemany("INSERT INTO sends (ig_user_id,kind,target,body,ok,at) VALUES (?,?,?,?,1,?)",
                    [(IG,"private_reply",f"p{i}","x",int(time.time())) for i in range(A.PRIVATE_REPLY_CAP_HOUR)])
asyncio.run(A.process(comment(cid="CAP")))
check("a rate-capped comment sends nothing", not q("SELECT 1 FROM sends WHERE target='CAP' AND ok=1"))
check("and is NOT left marked seen", not q("SELECT 1 FROM seen_events WHERE key='comment:CAP'"))
with A.db() as con:
    con.execute("DELETE FROM sends WHERE ok=1")        # the hour rolls over
asyncio.run(A.process(comment(cid="CAP")))
check("so the next poll actually delivers it", len(q("SELECT 1 FROM sends WHERE target='CAP' AND ok=1")) == 1)

reset()
with A.db() as con:
    con.executemany("INSERT INTO sends (ig_user_id,kind,target,body,ok,at) VALUES (?,?,?,?,0,?)",
                    [(IG,"private_reply","BAD","x",int(time.time())) for _ in range(3)])
asyncio.run(A.process(comment(cid="BAD")))
check("but a comment that has failed 3 times stops retrying",
      bool(q("SELECT 1 FROM seen_events WHERE key='comment:BAD'")))

reset()
asyncio.run(A.process(comment(cid="OLD"), ) if False else A.process({"object":"instagram","entry":[
    {"id":IG,"time":int(time.time())-8*86400,"changes":[{"field":"comments","value":{"id":"OLD","text":"GUIDE",
      "from":{"id":FAN,"username":"fan"}}}]}]}))
check("a >7-day comment stays seen (permanently ineligible, not retryable)",
      bool(q("SELECT 1 FROM seen_events WHERE key='comment:OLD'")))

print("\n3/4  email harvesting")
reset()
asyncio.run(A.process(comment(cid="C9")))              # normal comment, real IGSID known
asyncio.run(A.process(dm("my friend is alice@herclinic.com", mid="MX", sender="STRANGER")))
check("a stranger's DM does not leak a third party's email",
      not q("SELECT 1 FROM contacts WHERE igsid='STRANGER' AND email IS NOT NULL"))
check("and nothing is queued to the lead webhook", not q("SELECT 1 FROM lead_deliveries"))

reset()
asyncio.run(A.process(comment(cid="C8", frm=None, user="shy") if False else {"object":"instagram","entry":[
    {"id":IG,"time":int(time.time()),"changes":[{"field":"comments","value":{"id":"C8","text":"GUIDE",
      "from":{"id":"ig:shy","username":"shy"}}}]}]}))
check("an unresolvable commenter records exactly one open ask",
      len(q("SELECT 1 FROM unresolved_asks WHERE consumed=0")) == 1)
asyncio.run(A.process(dm("sure: shy@example.com", mid="M8", sender="REAL_ID")))
check("their DM is still captured", bool(q("SELECT 1 FROM contacts WHERE igsid='REAL_ID' AND email='shy@example.com'")))
check("the ask is consumed, not reusable", len(q("SELECT 1 FROM unresolved_asks WHERE consumed=0")) == 0)
asyncio.run(A.process(dm("and bob@other.com", mid="M8b", sender="ANOTHER")))
check("a second stranger cannot ride the same ask",
      not q("SELECT 1 FROM contacts WHERE igsid='ANOTHER' AND email IS NOT NULL"))

print("\n4/4  message budget, follow-ups, headers")
reset(ai_brief="b")
import sys
sys.modules['anthropic'] = types.SimpleNamespace(AsyncAnthropic=lambda **k: types.SimpleNamespace(
    beta=types.SimpleNamespace(messages=types.SimpleNamespace(
        create=lambda **kw: __import__('asyncio').sleep(0, types.SimpleNamespace(
            stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="x "*900)]))))))
asyncio.run(A.process(comment(cid="CB")))
body = q("SELECT body FROM sends WHERE target='CB'")[0]["body"]
check("a long AI reply is still capped", len(body.encode()) <= 1000)
check("and the email ask survives instead of being trimmed off", "your email?" in body)

reset(followup_text="nudge", followup_after=1)
asyncio.run(A.process(comment(cid="F1")))
asyncio.run(A.process(comment(cid="F2")))
check("two keyword comments schedule only ONE follow-up",
      len(q("SELECT 1 FROM followups WHERE state='pending'")) == 1)

reset(followup_text="nudge", followup_after=1)
asyncio.run(A.process(comment(cid="F3")))
asyncio.run(A.process(dm("not yet", mid="MF")))        # opens the 24h window
with A.db() as con:
    con.execute("UPDATE followups SET due_at=? WHERE state='pending'", (int(time.time())-1,))
_real = A.send_dm
async def boom(*a, **k): return False                  # transient Meta 500
A.send_dm = boom
with A.db() as con: asyncio.run(A.drain_followups(con))
A.send_dm = _real
row = q("SELECT * FROM followups")[0]
check("a transient failure does NOT expire the nudge", row["state"] == "pending")
check("it is rescheduled instead", row["due_at"] > int(time.time()))

# uvicorn decodes request headers as latin-1, so a non-ASCII byte reaches the handler
# as a str with codepoints > 127. TestClient refuses to encode one, so exercise the
# comparison directly - it used to raise TypeError out of the endpoint as a 500.
weird = b"\xc3\xa9".decode("latin-1")
check("comparing a non-ASCII header does not raise", A._eq(weird, "expected") is False)
check("a non-ASCII signature is rejected, not crashed",
      A.valid_signature(b"{}", f"sha256={weird}") is False)
check("constant-time compare still accepts a real match", A._eq("abc123", "abc123") is True)
check("and still rejects a near-miss", A._eq("abc123", "abc124") is False)

client = TestClient(A.app)
check("an ASCII bad signature is still a 401",
      client.post("/webhook", content=b'{"object":"instagram"}',
                  headers={"X-Hub-Signature-256": "sha256=deadbeef"}).status_code == 401)
check("admin still requires the token",
      client.get("/admin/stats", params={"ig_user_id": IG}).status_code == 401)
check("admin accepts the right token",
      client.get("/admin/stats", params={"ig_user_id": IG},
                 headers={"Authorization": "Bearer adm"}).status_code == 200)

print("\nall fix regressions pass")
