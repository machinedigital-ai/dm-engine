"""End-to-end check of the comment -> DM -> lead flow. Run: python test_app.py"""
import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time

os.environ.update(
    META_APP_SECRET="test-secret",
    META_VERIFY_TOKEN="test-verify",
    ADMIN_TOKEN="test-admin",
    META_DRY_RUN="1",
    DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"),
)

import app as A  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(A.app)
AUTH = {"Authorization": "Bearer test-admin"}
IG = "17841400000000000"
FAN = "9988776655"


def post_hook(payload: dict, secret: str = "test-secret"):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": sig})


def comment_event(comment_id: str, text: str, ts: int | None = None):
    return {
        "object": "instagram",
        "entry": [{
            "id": IG,
            "time": ts or int(time.time()),
            "changes": [{"field": "comments", "value": {
                "id": comment_id, "text": text,
                "from": {"id": FAN, "username": "jules.makes"},
            }}],
        }],
    }


def dm_event(mid: str, text: str):
    return {
        "object": "instagram",
        "entry": [{
            "id": IG,
            "time": int(time.time()),
            "messaging": [{
                "sender": {"id": FAN}, "recipient": {"id": IG},
                "timestamp": int(time.time() * 1000),
                "message": {"mid": mid, "text": text},
            }],
        }],
    }


def sends(kind=None):
    with A.db() as con:
        q = "SELECT * FROM sends" + (" WHERE kind=?" if kind else "")
        return [dict(r) for r in con.execute(q, (kind,) if kind else ())]


def contact():
    with A.db() as con:
        r = con.execute("SELECT * FROM contacts WHERE igsid=?", (FAN,)).fetchone()
        return dict(r) if r else None


def check(label, cond):
    assert cond, f"FAILED: {label}"
    print(f"  ok  {label}")


def main():
    with A.db() as con:
        A.ensure(con)

    # --- trust boundary -------------------------------------------------
    check("unsigned webhook is rejected",
          client.post("/webhook", json=comment_event("c0", "GUIDE")).status_code == 401)
    check("wrong-signature webhook is rejected",
          post_hook(comment_event("c0", "GUIDE"), secret="wrong").status_code == 401)
    check("admin endpoints require a token",
          client.get("/admin/stats", params={"ig_user_id": IG}).status_code == 401)
    check("verify handshake echoes the challenge",
          client.get("/webhook", params={"hub.mode": "subscribe",
                                         "hub.verify_token": "test-verify",
                                         "hub.challenge": "42"}).text == "42")
    check("verify handshake rejects a bad token",
          client.get("/webhook", params={"hub.mode": "subscribe",
                                         "hub.verify_token": "nope",
                                         "hub.challenge": "42"}).status_code == 403)

    # --- setup ----------------------------------------------------------
    client.post("/admin/accounts", headers=AUTH,
                json={"ig_user_id": IG, "username": "nightshift", "access_token": "tok"})
    client.post("/admin/automations", headers=AUTH, json={
        "ig_user_id": IG, "keyword": "GUIDE",
        "private_reply": "Here you go: nightshift.co/guide",
        "ask_email": "Want a copy by email too? Drop your address.",
        "email_thanks": "Sent. Check your inbox.",
    })

    # --- the money flow -------------------------------------------------
    check("unmatched comment sends nothing",
          post_hook(comment_event("c1", "love this")).status_code == 200 and not sends())

    post_hook(comment_event("c2", "GUIDE please"))
    pr = sends("private_reply")
    check("keyword comment triggers exactly one private reply", len(pr) == 1 and pr[0]["ok"] == 1)
    check("link and email ask ride in ONE message (Meta allows one per comment)",
          "nightshift.co/guide" in pr[0]["body"] and "Drop your address" in pr[0]["body"])
    check("contact is now awaiting an email", contact()["state"] == "awaiting_email")

    post_hook(comment_event("c2", "GUIDE please"))
    check("redelivered comment is deduped", len(sends("private_reply")) == 1)

    post_hook(dm_event("m1", "sure, its not-an-email"))
    check("garbage answer is re-asked, not stored",
          contact()["email"] is None and contact()["email_asks"] == 2)

    post_hook(dm_event("m2", "ok its jules@example.com thanks"))
    check("email is parsed and stored", contact()["email"] == "jules@example.com")
    check("contact flow is complete", contact()["state"] == "done")
    check("confirmation DM went out", any("Check your inbox" in s["body"] for s in sends("dm")))

    post_hook(dm_event("m2", "duplicate"))
    check("redelivered DM is deduped", len([s for s in sends("dm") if "Check your inbox" in s["body"]]) == 1)

    r = client.get("/admin/leads", headers=AUTH, params={"ig_user_id": IG}).json()
    check("lead shows up in the admin list", r["leads"][0]["email"] == "jules@example.com")

    # --- Meta's ceilings ------------------------------------------------
    old = int(time.time()) - 8 * 24 * 3600
    post_hook(comment_event("c3", "GUIDE", ts=old))
    check("comment older than 7 days is refused",
          any(s["error"] == "comment older than 7 days" for s in sends()))

    with A.db() as con:
        ok = asyncio.run(
            A.send_dm(con, A.get_account(con, IG), FAN, "stale", int(time.time()) - 25 * 3600))
    check("DM outside the 24h window is refused", ok is False)
    check("refusal reason is recorded",
          any(s["error"] == "outside 24h messaging window" for s in sends("dm")))

    with A.db() as con:
        now = int(time.time())
        con.executemany(
            "INSERT INTO sends (ig_user_id,kind,target,body,ok,error,at) VALUES (?,?,?,?,1,NULL,?)",
            [(IG, "private_reply", f"pad{i}", "x", now) for i in range(A.PRIVATE_REPLY_CAP_HOUR)],
        )
    before = len(sends("private_reply"))
    post_hook(comment_event("c4", "GUIDE"))
    after = [s for s in sends("private_reply") if s["error"] == "local rate cap 750/hr"]
    check("private replies stop at Meta's 750/hour cap", len(after) == 1)
    check("the blocked attempt is logged, not silently dropped", len(sends("private_reply")) == before + 1)

    s = client.get("/admin/stats", headers=AUTH, params={"ig_user_id": IG}).json()
    check("stats report the cap", s["private_reply_cap"] == 750 and s["leads"] == 1)

    print("\nall checks passed")


if __name__ == "__main__":
    main()
