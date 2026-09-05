"""AI replies, follow-ups, lead delivery, byte caps. All external calls mocked."""
import asyncio, json, os, tempfile, time, types
os.environ.update(META_APP_SECRET="s", META_VERIFY_TOKEN="v", META_DRY_RUN="1",
                  ANTHROPIC_API_KEY="test-key", LEAD_WEBHOOK_URL="https://hook.example/lead",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "f.db"))
import app as A

IG, FAN = "IG1", "FAN1"
posted = []

def check(l, c):
    assert c, f"FAILED: {l}"; print(f"  ok  {l}")

def reset(**auto):
    with A.db() as con:
        A.ensure(con)
        for t in ("accounts","automations","contacts","sends","seen_events","followups","lead_deliveries"):
            con.execute(f"DELETE FROM {t}")
        con.execute("INSERT INTO accounts VALUES (?,?,?,?,?)", (IG,"me","tok",int(time.time())+86400,int(time.time())))
        cols = dict(ig_user_id=IG, keyword="GUIDE", private_reply="canned: x.co/g",
                    ask_email="your email?", email_thanks="thanks!", public_reply=None,
                    ai_brief=None, followup_text=None, followup_after=None)
        cols.update(auto)
        con.execute(f"INSERT INTO automations ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                    tuple(cols.values()))

def comment(text="GUIDE please", cid=None):
    return {"object":"instagram","entry":[{"id":IG,"time":int(time.time()),"changes":[
        {"field":"comments","value":{"id":cid or f"C{time.time()}","text":text,
         "from":{"id":FAN,"username":"fan"}}}]}]}

def dm(text, mid=None):
    return {"object":"instagram","entry":[{"id":IG,"time":int(time.time()),"messaging":[
        {"sender":{"id":FAN},"recipient":{"id":IG},"timestamp":int(time.time()*1000),
         "message":{"mid":mid or f"M{time.time()}","text":text}}]}]}

def sends(kind=None):
    with A.db() as con:
        q="SELECT * FROM sends"+(" WHERE kind=?" if kind else "")+" ORDER BY id"
        return [dict(r) for r in con.execute(q,(kind,) if kind else ())]

# ---------------- fake Anthropic ----------------
class FakeMsg:
    def __init__(self, text=None, stop="end_turn"):
        self.stop_reason = stop
        self.content = [types.SimpleNamespace(type="text", text=text)] if text else []

def fake_anthropic(behaviour):
    captured = {}
    class Beta:
        class messages:
            @staticmethod
            async def create(**kw):
                captured.update(kw)
                if isinstance(behaviour, Exception): raise behaviour
                return behaviour
    mod = types.SimpleNamespace(
        AsyncAnthropic=lambda **kw: types.SimpleNamespace(beta=Beta))
    return mod, captured

_real_import = __builtins__.__import__ if hasattr(__builtins__,'__import__') else __import__
def patch_anthropic(behaviour):
    mod, captured = fake_anthropic(behaviour)
    import sys; sys.modules['anthropic'] = mod
    return captured

# ---------------- byte cap ----------------
print("byte cap")
check("short text is untouched", A.fit("hello") == "hello")
long = "word " * 500
check("long text is cut to the limit", len(A.fit(long).encode()) <= 1000)
check("cut text is marked", A.fit(long).endswith("..."))
check("emoji are counted as bytes not characters", len(A.fit("\U0001F600"*400).encode()) <= 1000)

# ---------------- AI replies ----------------
print("\nAI replies")
reset(ai_brief="We sell handmade candles. Shipping is 3 days in the UK.")
cap = patch_anthropic(FakeMsg("Yes, UK shipping is 3 days. Here you go: x.co/g"))
asyncio.run(A.process(comment("GUIDE - do you ship to the UK?")))
body = sends("private_reply")[0]["body"]
check("AI text is used instead of the canned reply", "UK shipping is 3 days" in body)
check("the email ask is still appended", "your email?" in body)
check("model is claude-opus-5", cap["model"] == "claude-opus-5")
check("effort is low for a two-sentence DM", cap["output_config"]["effort"] == "low")
check("refusal fallbacks are enabled", cap["fallbacks"] == "default")
check("the operator brief is in the system prompt",
      any("handmade candles" in b["text"] for b in cap["system"]))
sent_user = cap["messages"][0]["content"]
check("the comment is delimited as untrusted data", "<<<COMMENT" in sent_user and "COMMENT>>>" in sent_user)
check("the system prompt forbids treating the comment as instructions",
      "never instructions" in A.AI_SYSTEM.lower() or "not instructions" in A.AI_SYSTEM.lower())

reset(ai_brief="brief")
patch_anthropic(RuntimeError("api down"))
asyncio.run(A.process(comment("GUIDE")))
check("an API failure falls back to the canned reply", "canned:" in sends("private_reply")[0]["body"])

reset(ai_brief="brief")
patch_anthropic(FakeMsg(None, stop="refusal"))
asyncio.run(A.process(comment("GUIDE")))
check("a refusal falls back to the canned reply", "canned:" in sends("private_reply")[0]["body"])

reset(ai_brief="brief")
patch_anthropic(FakeMsg("x " * 4000))
asyncio.run(A.process(comment("GUIDE")))
check("an over-long AI reply is trimmed to Meta's cap",
      len(sends("private_reply")[0]["body"].encode()) <= 1000)

reset()  # no ai_brief
cap = patch_anthropic(FakeMsg("should not be used"))
asyncio.run(A.process(comment("GUIDE")))
check("without a brief the model is never called", not cap and "canned:" in sends("private_reply")[0]["body"])

# ---------------- follow-ups ----------------
print("\nfollow-ups")
reset(followup_text="still want that guide?", followup_after=2)
asyncio.run(A.process(comment("GUIDE")))
with A.db() as con:
    f = con.execute("SELECT * FROM followups").fetchall()
check("a follow-up is scheduled after the first reply", len(f) == 1 and f[0]["state"] == "pending")
check("it is due later, not now", f[0]["due_at"] > int(time.time()))

with A.db() as con:
    con.execute("UPDATE followups SET due_at=?", (int(time.time()) - 1,))
    n = asyncio.run(A.drain_followups(con))
    st = con.execute("SELECT state FROM followups").fetchone()["state"]
check("a nudge to someone who never replied cannot send", n == 0 and st == "expired")
check("and the reason is logged, not swallowed",
      any(s["error"] == "outside 24h messaging window" for s in sends("dm")))

reset(followup_text="still want it?", followup_after=2)
asyncio.run(A.process(comment("GUIDE")))
asyncio.run(A.process(dm("hmm not sure")))          # they reply -> window opens
with A.db() as con:
    con.execute("UPDATE followups SET due_at=? WHERE state='pending'", (int(time.time()) - 1,))
    n = asyncio.run(A.drain_followups(con))
check("a nudge DOES send once they have replied", n == 1)
check("the nudge text went out", any("still want it?" in s["body"] for s in sends("dm")))

reset(followup_text="still want it?", followup_after=2)
asyncio.run(A.process(comment("GUIDE")))
asyncio.run(A.process(dm("sure, me@example.com")))
with A.db() as con:
    st = con.execute("SELECT state FROM followups").fetchone()["state"]
check("giving an email cancels the pending nudge", st == "cancelled")

# ---------------- lead delivery ----------------
print("\nlead delivery")
class FakeResp:
    def __init__(self, code=200): self.status_code, self.text = code, "ok"
class FakeClient:
    def __init__(self, code=200, boom=False): self.code, self.boom = code, boom
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def post(self, url, json=None, **kw):
        posted.append((url, json))
        if self.boom: raise RuntimeError("connection refused")
        return FakeResp(self.code)

posted.clear()
A.httpx.AsyncClient = lambda **kw: FakeClient()
reset()
asyncio.run(A.process(comment("GUIDE")))
asyncio.run(A.process(dm("here: buyer@example.com")))
check("the lead is POSTed to the webhook", len(posted) == 1)
url, payload = posted[0]
check("it goes to the configured URL", url == "https://hook.example/lead")
check("the payload carries the email", payload["email"] == "buyer@example.com")
check("and the keyword that produced it", payload["keyword"] == "GUIDE")
with A.db() as con:
    check("delivery is marked sent",
          con.execute("SELECT state FROM lead_deliveries").fetchone()["state"] == "sent")

posted.clear()
A.httpx.AsyncClient = lambda **kw: FakeClient(boom=True)
reset()
asyncio.run(A.process(comment("GUIDE")))
asyncio.run(A.process(dm("here: retry@example.com")))
with A.db() as con:
    r = con.execute("SELECT * FROM lead_deliveries").fetchone()
check("a failed POST stays pending, not lost", r["state"] == "pending" and r["attempts"] == 1)
check("the error is recorded", "connection refused" in r["last_error"])
with A.db() as con:
    check("the lead is still saved locally regardless",
          con.execute("SELECT email FROM contacts").fetchone()["email"] == "retry@example.com")

A.httpx.AsyncClient = lambda **kw: FakeClient()
with A.db() as con:
    n = asyncio.run(A.drain_lead_deliveries(con))
check("an immediate retry is held back by the backoff", n == 0)
with A.db() as con:
    con.execute("UPDATE lead_deliveries SET at=at-120")     # 2 min later
    n = asyncio.run(A.drain_lead_deliveries(con))
    st = con.execute("SELECT state FROM lead_deliveries").fetchone()["state"]
check("once the backoff elapses it delivers", n == 1 and st == "sent")

with A.db() as con:
    con.execute("UPDATE lead_deliveries SET state='pending', attempts=5")
    n = asyncio.run(A.drain_lead_deliveries(con))
check("retries stop after 5 attempts instead of hammering forever", n == 0)

# ---------------- migration ----------------
print("\nmigration")
import sqlite3
old = os.path.join(tempfile.mkdtemp(), "old.db")
c = sqlite3.connect(old)
c.execute("CREATE TABLE automations (id INTEGER PRIMARY KEY, ig_user_id TEXT, keyword TEXT)")
c.execute("CREATE TABLE contacts (ig_user_id TEXT, igsid TEXT)")
c.execute("INSERT INTO automations (ig_user_id,keyword) VALUES ('X','OLD')")
c.commit()
c.row_factory = sqlite3.Row
A.migrate(c)
cols = {r["name"] for r in c.execute("PRAGMA table_info(automations)")}
check("an old database gains the new columns", {"ai_brief","followup_text","followup_after"} <= cols)
check("existing rows survive", c.execute("SELECT keyword FROM automations").fetchone()["keyword"] == "OLD")
A.migrate(c)
check("migrating twice is safe", True)

print("\nall feature checks passed")
