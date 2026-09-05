"""Prove the no-webhook polling path works, with Meta's HTTP mocked out."""
import os, tempfile, time, types
os.environ.update(META_APP_SECRET="s", META_VERIFY_TOKEN="v", META_DRY_RUN="1",
                  DB_PATH=os.path.join(tempfile.mkdtemp(), "p.db"))
import app as A, cli, argparse, httpx

IG = "17841400000000000"
now = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime())
old = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(time.time() - 9*86400))

def fake_get(url, **kw):
    r = types.SimpleNamespace(status_code=200)
    if url.endswith("/me/media"):
        r.json = lambda: {"data": [{"id": "MEDIA_1", "timestamp": now}]}
    elif "/comments" in url:
        r.json = lambda: {"data": [
            {"id": "C_FRESH", "text": "GUIDE please", "timestamp": now,
             "username": "fan", "from": {"id": "FAN_ID", "username": "fan"}},
            {"id": "C_NOFROM", "text": "GUIDE!", "timestamp": now, "username": "shy_fan"},
            {"id": "C_STALE", "text": "GUIDE", "timestamp": old, "username": "late"},
            {"id": "C_NOMATCH", "text": "nice post", "timestamp": now, "username": "someone"},
        ]}
    return r
cli.httpx = types.SimpleNamespace(get=fake_get, post=httpx.post)

def check(l, c):
    assert c, f"FAILED: {l}"; print(f"  ok  {l}")

with A.db() as con:
    con.executescript(A.SCHEMA)
    con.execute("INSERT INTO accounts VALUES (?,?,?,?,?)", (IG,"me","tok",int(time.time())+86400,int(time.time())))
    con.execute("INSERT INTO automations (ig_user_id,keyword,private_reply,ask_email,email_thanks,public_reply)"
                " VALUES (?,?,?,?,?,NULL)", (IG,"GUIDE","link: x.co/g","send your email","thanks!"))

cli.cmd_poll(argparse.Namespace(once=True, interval=60, media=10))
with A.db() as con:
    sends = [dict(r) for r in con.execute("SELECT * FROM sends")]
    targets = {s["target"] for s in sends}

check("fresh matching comment gets a private reply", "C_FRESH" in targets)
check("comment with no 'from' still gets replied to (comment_id is the recipient)", "C_NOFROM" in targets)
check("comment older than 7 days is skipped", "C_STALE" not in targets)
check("non-matching comment is ignored", "C_NOMATCH" not in targets)
check("exactly the two matches fired", len(sends) == 2)

cli.cmd_poll(argparse.Namespace(once=True, interval=60, media=10))
with A.db() as con:
    check("re-polling the same comments sends nothing new",
          con.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 2)

# the degraded path: commenter DMs an email, but we never learned their IGSID
import asyncio, json
asyncio.run(A.process({"object":"instagram","entry":[{"id":IG,"time":int(time.time()),"messaging":[
    {"sender":{"id":"REAL_IGSID_WE_NEVER_SAW"},"recipient":{"id":IG},
     "timestamp":int(time.time()*1000),"message":{"mid":"M1","text":"here: shy@example.com"}}]}]}))
with A.db() as con:
    c = con.execute("SELECT * FROM contacts WHERE igsid='REAL_IGSID_WE_NEVER_SAW'").fetchone()
check("lead is still captured when the IGSID never matched a contact", c and c["email"] == "shy@example.com")

# and the guard holds: an unsolicited email from nowhere is not harvested
with A.db() as con:
    con.execute("UPDATE sends SET at=? WHERE kind='private_reply'", (int(time.time()) - 40*3600,))
asyncio.run(A.process({"object":"instagram","entry":[{"id":IG,"time":int(time.time()),"messaging":[
    {"sender":{"id":"RANDOM"},"recipient":{"id":IG},
     "timestamp":int(time.time()*1000),"message":{"mid":"M2","text":"contact me at spam@example.com"}}]}]}))
with A.db() as con:
    c2 = con.execute("SELECT * FROM contacts WHERE igsid='RANDOM'").fetchone()
check("an email with no recent ask is NOT harvested", c2 and c2["email"] is None)

print("\npolling path verified")
