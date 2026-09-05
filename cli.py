#!/usr/bin/env python3
"""Single-operator control for dm-engine. Talks straight to the DB - no admin token.

    python3 cli.py connect <token>      register your account (resolves the ID for you)
    python3 cli.py keyword GUIDE ...    add a comment/DM automation
    python3 cli.py test GUIDE           fire a fake comment through the real handler
    python3 cli.py poll                 no-webhook mode: watch comments by polling
    python3 cli.py install              same, but in the background from login, forever
    python3 cli.py leads | log | stats  see what happened
    python3 cli.py selftest             prove the whole local chain works
    python3 cli.py doctor               is anything obviously wrong
    python3 cli.py refresh              extend the 60-day token
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time

import httpx

import app as A


def _me(token: str) -> dict:
    r = httpx.get(
        f"{A.GRAPH_BASE}/me",
        params={"fields": "user_id,username", "access_token": token},
        timeout=15,
    )
    if r.status_code >= 400:
        sys.exit(f"Meta rejected the token: {r.status_code} {r.text[:300]}")
    return r.json()


def cmd_connect(args) -> None:
    me = _me(args.token)
    ig_id, username = str(me.get("user_id") or me.get("id")), me.get("username")
    with A.db() as con:
        A.ensure(con)
        con.execute(
            "INSERT OR REPLACE INTO accounts (ig_user_id,username,access_token,token_expires_at,created_at)"
            " VALUES (?,?,?,?,?)",
            (ig_id, username, args.token, int(time.time()) + 60 * 86400, int(time.time())),
        )
    print(f"connected  @{username}  ig_user_id={ig_id}")
    print("token assumed good for 60 days - run `cli.py refresh` monthly")


def cmd_keyword(args) -> None:
    with A.db() as con:
        A.ensure(con)
        acct = con.execute("SELECT * FROM accounts LIMIT 1").fetchone()
        if not acct:
            sys.exit("no account yet - run: python3 cli.py connect <token>")
        cur = con.execute(
            "INSERT INTO automations (ig_user_id,keyword,private_reply,ask_email,email_thanks,public_reply)"
            " VALUES (?,?,?,?,?,?)",
            (acct["ig_user_id"], args.keyword, args.reply, args.ask_email, args.thanks, args.public),
        )
        if args.ai_brief or args.followup:
            con.execute(
                "UPDATE automations SET ai_brief=?, followup_text=?, followup_after=? WHERE id=?",
                (args.ai_brief, args.followup, (args.followup_after or 4) * 3600, cur.lastrowid),
            )
    print(f"automation #{cur.lastrowid}  keyword={args.keyword!r} on @{acct['username']}")
    if args.ask_email:
        budget = len(args.reply.encode()) + 2 + len(args.ask_email.encode())
        flag = "  <-- over Meta's 1000-byte limit, it will be trimmed" if budget > 1000 else ""
        print(f"  reply + email ask = {budget} bytes in one message{flag}")
    if args.ai_brief:
        print("  mode: AI - the canned reply is the fallback when the model is unavailable")
        if not A.ANTHROPIC_API_KEY:
            print("  WARNING: ANTHROPIC_API_KEY is not set, so it will always use the canned reply")
    if args.followup:
        print(f"  follow-up: after {args.followup_after or 4}h, only if they replied "
              f"(Meta's 24h window) and still have not given an email")


def cmd_list(args) -> None:
    with A.db() as con:
        for a in con.execute("SELECT * FROM automations ORDER BY id"):
            state = "on " if a["active"] else "off"
            mode = "AI " if a["ai_brief"] else "   "
            print(f"#{a['id']} [{state}] {mode}{a['keyword']:<14} {a['private_reply'][:56]}")
            if a["followup_text"]:
                print(f"          follow-up +{(a['followup_after'] or 0)//3600}h: {a['followup_text'][:52]}")


def cmd_test(args) -> None:
    """Push a synthetic comment through the real handler. Honours META_DRY_RUN."""
    with A.db() as con:
        A.ensure(con)
        acct = con.execute("SELECT * FROM accounts LIMIT 1").fetchone()
        if not acct:
            sys.exit("no account yet - run: python3 cli.py connect <token>")
        ig_id = acct["ig_user_id"]
    payload = {
        "object": "instagram",
        "entry": [{
            "id": ig_id,
            "time": int(time.time()),
            "changes": [{"field": "comments", "value": {
                "id": f"test-{int(time.time())}",
                "text": args.text,
                "from": {"id": "test-commenter", "username": "test_user"},
            }}],
        }],
    }
    asyncio.run(A.process(payload))
    print(f"pushed comment {args.text!r} through the handler:\n")
    cmd_log(argparse.Namespace(n=5))
    if A.DRY_RUN:
        print("\n(META_DRY_RUN=1 - nothing was actually sent to Instagram)")


def cmd_leads(args) -> None:
    with A.db() as con:
        rows = con.execute(
            "SELECT username,email,created_at FROM contacts WHERE email IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        return print("no leads yet")
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["created_at"]))
        print(f"{when}  {r['email']:<34} @{r['username'] or '?'}")
    print(f"\n{len(rows)} lead(s)")


def cmd_log(args) -> None:
    with A.db() as con:
        rows = con.execute("SELECT * FROM sends ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    if not rows:
        return print("nothing sent yet")
    for r in reversed(rows):
        when = time.strftime("%H:%M:%S", time.localtime(r["at"]))
        mark = "ok  " if r["ok"] else "FAIL"
        tail = f"  <- {r['error']}" if r["error"] else ""
        print(f"{when} {mark} {r['kind']:<14} {r['body'][:56]!r}{tail}")


def cmd_stats(args) -> None:
    with A.db() as con:
        hour = int(time.time()) - 3600
        used = con.execute(
            "SELECT COUNT(*) FROM sends WHERE kind='private_reply' AND ok=1 AND at>?", (hour,)
        ).fetchone()[0]
        print(f"private replies this hour  {used} / {A.PRIVATE_REPLY_CAP_HOUR}")
        print(f"contacts                   {con.execute('SELECT COUNT(*) FROM contacts').fetchone()[0]}")
        print(f"leads                      {con.execute('SELECT COUNT(*) FROM contacts WHERE email IS NOT NULL').fetchone()[0]}")
        print(f"failed sends               {con.execute('SELECT COUNT(*) FROM sends WHERE ok=0').fetchone()[0]}")


def cmd_tick(args) -> None:
    with A.db() as con:
        A.ensure(con)
        r = asyncio.run(A.tick(con))
    print(f"follow-ups sent: {r['followups_sent']}   leads delivered: {r['leads_delivered']}")


def cmd_refresh(args) -> None:
    with A.db() as con:
        acct = con.execute("SELECT * FROM accounts LIMIT 1").fetchone()
        if not acct:
            sys.exit("no account yet")
        r = httpx.get(
            f"{A.GRAPH_BASE}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": acct["access_token"]},
            timeout=15,
        )
        if r.status_code >= 400:
            sys.exit(f"refresh failed: {r.status_code} {r.text[:300]}\nRe-generate the token in the dashboard.")
        data = r.json()
        expires = int(time.time()) + int(data.get("expires_in", 60 * 86400))
        con.execute(
            "UPDATE accounts SET access_token=?, token_expires_at=? WHERE ig_user_id=?",
            (data["access_token"], expires, acct["ig_user_id"]),
        )
    print(f"token refreshed, good until {time.strftime('%Y-%m-%d', time.localtime(expires))}")


LABEL = "com.dm-engine.poll"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _here() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def cmd_install(args) -> None:
    """Run `cli.py poll` as a macOS LaunchAgent: starts at login, restarts if it dies."""
    py = os.path.join(_here(), ".venv", "bin", "python")
    if not os.path.exists(py):
        sys.exit("no .venv here - run:  python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt")
    log_path = os.path.join(_here(), "poll.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{py}</string><string>{os.path.join(_here(), "cli.py")}</string>
    <string>poll</string><string>--interval</string><string>{args.interval}</string>
  </array>
  <key>WorkingDirectory</key><string>{_here()}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{log_path}</string>
  <key>StandardErrorPath</key><string>{log_path}</string>
</dict></plist>
"""
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    open(PLIST, "w").write(plist)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", PLIST], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"launchctl refused: {r.stderr.strip() or r.stdout.strip()}")
    print(f"installed {LABEL}")
    print(f"  polls every {args.interval}s, starts at login, restarts if it dies")
    print(f"  log: {log_path}")
    print(f"  stop: python3 cli.py uninstall")


def cmd_uninstall(args) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", PLIST], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
        print(f"removed {LABEL}")
    else:
        print("nothing installed")


def _service_running() -> bool:
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    return r.returncode == 0 and "state = running" in r.stdout


def cmd_poll(args) -> None:
    """Trigger source #2: poll your own media for new comments.

    Webhooks are documented as needing Advanced Access; private replies are not.
    Polling reaches the same comment_id with only instagram_business_basic +
    instagram_business_manage_comments, so this path needs no App Review.
    Meta allows a private reply within 7 DAYS of a comment, so a slow poll is fine.
    """
    with A.db() as con:
        A.ensure(con)
        acct = con.execute("SELECT * FROM accounts LIMIT 1").fetchone()
    if not acct:
        sys.exit("no account yet - run: python3 cli.py connect <token>")
    token = acct["access_token"]

    def cycle() -> int:
        handled = 0
        r = httpx.get(f"{A.GRAPH_BASE}/me/media", timeout=20,
                      params={"fields": "id,timestamp", "limit": args.media, "access_token": token})
        if r.status_code >= 400:
            print(f"  media list failed: {r.status_code} {r.text[:200]}")
            return 0
        cutoff = time.time() - A.COMMENT_WINDOW
        for m in r.json().get("data", []):
            rc = httpx.get(f"{A.GRAPH_BASE}/{m['id']}/comments", timeout=20,
                           params={"fields": "id,text,timestamp,username,from", "access_token": token})
            if rc.status_code >= 400:
                print(f"  comments on {m['id']} failed: {rc.status_code} {rc.text[:200]}")
                continue
            for c in rc.json().get("data", []):
                ts = _ts(c.get("timestamp"))
                if ts < cutoff:
                    continue
                frm = c.get("from") or {}
                value = {
                    "id": c["id"],
                    "text": c.get("text", ""),
                    # no 'from.id' when Meta withholds it - fall back to the handle so the
                    # private reply (which targets comment_id, not a user id) still goes out
                    "from": {"id": frm.get("id") or f"ig:{c.get('username','unknown')}",
                             "username": frm.get("username") or c.get("username")},
                }
                with A.db() as con:
                    before = con.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
                    asyncio.run(A.handle_comment(con, acct["ig_user_id"], value, int(ts)))
                    after = con.execute("SELECT COUNT(*) FROM sends").fetchone()[0]
                handled += after - before
        return handled

    def drain() -> dict:
        with A.db() as con:
            return asyncio.run(A.tick(con))

    if args.once:
        n = cycle()
        d = drain()
        if d["followups_sent"] or d["leads_delivered"]:
            print(f"  follow-ups sent: {d['followups_sent']}, leads delivered: {d['leads_delivered']}")
        print(f"polled once - {n} new action(s)")
        return cmd_log(argparse.Namespace(n=5)) if n else None

    print(f"polling every {args.interval}s. Ctrl-C to stop.")
    while True:
        n = cycle()
        drain()
        if n:
            print(f"[{time.strftime('%H:%M:%S')}] {n} new action(s)")
            cmd_log(argparse.Namespace(n=n))
        time.sleep(args.interval)


def _ts(iso: str | None) -> float:
    """Meta returns e.g. 2026-09-01T12:00:00+0000."""
    if not iso:
        return time.time()
    try:
        return __import__("datetime").datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except ValueError:
        return time.time()


TEST_IG = "__selftest__"
TEST_FAN = "__selftest_follower__"


def _signed(url: str, payload: dict) -> httpx.Response:
    raw = __import__("json").dumps(payload).encode()
    sig = "sha256=" + __import__("hmac").new(
        A.APP_SECRET.encode(), raw, __import__("hashlib").sha256).hexdigest()
    return httpx.post(url, content=raw, timeout=10,
                      headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def cmd_selftest(args) -> None:
    """Prove the server, signature check and full flow work - before Meta is involved."""
    base = args.url.rstrip("/")
    ok = True
    step = 0

    def chk(label, cond, fix=""):
        nonlocal ok, step
        step += 1
        ok = ok and cond
        print(f"  {step}. {'PASS' if cond else 'FAIL'}  {label}" + ("" if cond else f"\n            -> {fix}"))
        return cond

    if not A.APP_SECRET:
        sys.exit("META_APP_SECRET is not set. Run:  set -a && . ./.env && set +a")

    print(f"testing {base}\n")
    try:
        h = httpx.get(f"{base}/healthz", timeout=5).json()
    except Exception:
        sys.exit(f"Server is not answering on {base}.\n"
                 f"  Start it in another terminal:  ./.venv/bin/uvicorn app:app --port 8000")
    chk("server is up", h.get("ok") is True)
    print(f"            META_DRY_RUN={'1 - nothing reaches Instagram' if h.get('dry_run') else '0 - LIVE'}")

    r = httpx.get(f"{base}/webhook", timeout=5, params={
        "hub.mode": "subscribe", "hub.verify_token": A.VERIFY_TOKEN, "hub.challenge": "OK42"})
    chk("Meta's verification handshake answers correctly", r.text == "OK42",
        "META_VERIFY_TOKEN differs between your shell and the running server. Restart uvicorn.")

    r = httpx.get(f"{base}/webhook", timeout=5, params={
        "hub.mode": "subscribe", "hub.verify_token": "wrong-on-purpose", "hub.challenge": "x"})
    chk("a wrong verify token is rejected", r.status_code == 403)

    r = httpx.post(f"{base}/webhook", json={"object": "instagram"}, timeout=5)
    chk("an unsigned webhook is rejected", r.status_code == 401)

    r = _signed(f"{base}/webhook", {"object": "instagram", "entry": []})
    chk("a correctly signed webhook is accepted", r.status_code == 200,
        "META_APP_SECRET differs between your shell and the running server. Restart uvicorn.")

    with A.db() as con:
        A.ensure(con)
        con.execute("DELETE FROM accounts WHERE ig_user_id=?", (TEST_IG,))
        con.execute("INSERT INTO accounts VALUES (?,?,?,?,?)",
                    (TEST_IG, "selftest", "fake", int(time.time()) + 86400, int(time.time())))
        con.execute("INSERT INTO automations (ig_user_id,keyword,private_reply,ask_email,email_thanks,public_reply)"
                    " VALUES (?,?,?,?,?,NULL)",
                    (TEST_IG, "SELFTEST", "here is the link", "drop your email", "got it"))
    t = int(time.time())
    try:
        _signed(f"{base}/webhook", {"object": "instagram", "entry": [{
            "id": TEST_IG, "time": t, "changes": [{"field": "comments", "value": {
                "id": f"selftest-c-{t}", "text": "SELFTEST please",
                "from": {"id": TEST_FAN, "username": "selftest_user"}}}]}]})
        time.sleep(1.2)
        with A.db() as con:
            sent = con.execute("SELECT * FROM sends WHERE ig_user_id=? AND kind='private_reply'",
                               (TEST_IG,)).fetchall()
        chk("keyword comment triggers a private reply", len(sent) == 1 and sent[0]["ok"] == 1,
            sent and sent[0]["error"] or "check the uvicorn terminal for a traceback")
        if sent:
            chk("link and email ask ship as ONE message",
                "here is the link" in sent[0]["body"] and "drop your email" in sent[0]["body"])

        _signed(f"{base}/webhook", {"object": "instagram", "entry": [{
            "id": TEST_IG, "time": t, "messaging": [{
                "sender": {"id": TEST_FAN}, "recipient": {"id": TEST_IG},
                "timestamp": t * 1000,
                "message": {"mid": f"selftest-m-{t}", "text": "sure: probe@example.com"}}]}]})
        time.sleep(1.2)
        with A.db() as con:
            c = con.execute("SELECT * FROM contacts WHERE ig_user_id=?", (TEST_IG,)).fetchone()
        chk("email in a DM reply is captured as a lead", bool(c) and c["email"] == "probe@example.com")
    finally:
        with A.db() as con:
            for tbl in ("sends", "contacts", "automations", "accounts"):
                con.execute(f"DELETE FROM {tbl} WHERE ig_user_id=?", (TEST_IG,))

    print("\n" + ("everything local works. Next: expose it with a tunnel and point Meta at it."
                  if ok else "fix the FAILs above, then re-run"))
    sys.exit(0 if ok else 1)


def cmd_doctor(args) -> None:
    ok = True

    def chk(label, cond, fix=""):
        nonlocal ok
        ok = ok and cond
        print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"\n         -> {fix}"))

    print("config")
    chk("META_APP_SECRET set", bool(A.APP_SECRET) and A.APP_SECRET != "replace-me",
        "still the placeholder from .env.example. Instagram > API setup > Instagram app secret")
    chk("META_VERIFY_TOKEN set", bool(A.VERIFY_TOKEN) and A.VERIFY_TOKEN != "replace-me",
        "still the placeholder from .env.example. Any string you invent, matched in the dashboard")
    print(f"  --   META_DRY_RUN={'1 (nothing reaches Instagram)' if A.DRY_RUN else '0 (LIVE)'}")

    print("\naccount")
    with A.db() as con:
        A.ensure(con)
        acct = con.execute("SELECT * FROM accounts LIMIT 1").fetchone()
        n_auto = con.execute("SELECT COUNT(*) FROM automations WHERE active=1").fetchone()[0]
    chk("account connected", bool(acct), "python3 cli.py connect <token>")
    chk("at least one active automation", n_auto > 0, "python3 cli.py keyword GUIDE --reply '...'")

    if acct:
        left = (acct["token_expires_at"] or 0) - int(time.time())
        chk(f"token has {max(0, left)//86400}d left", left > 7 * 86400, "python3 cli.py refresh")
        try:
            me = _me(acct["access_token"])
            chk(f"Meta accepts the token (@{me.get('username')})", True)
        except SystemExit:
            chk("Meta accepts the token", False, "token revoked or expired - regenerate in the dashboard")

    print("\nbackground poller")
    if os.path.exists(PLIST):
        print(f"  {'ok  ' if _service_running() else 'FAIL'} installed and "
              f"{'running' if _service_running() else 'NOT running - check poll.log'}")
    else:
        print("  --   not installed (python3 cli.py install)")

    print("\n" + ("all good" if ok else "fix the FAILs above, then re-run"))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("connect", help="register your Instagram account"); c.add_argument("token"); c.set_defaults(fn=cmd_connect)
    k = sub.add_parser("keyword", help="add an automation")
    k.add_argument("keyword")
    k.add_argument("--reply", required=True, help="the DM they get (include your link)")
    k.add_argument("--ask-email", dest="ask_email", help="appended to the same message; omit to skip lead capture")
    k.add_argument("--thanks", help="sent once they hand over an email")
    k.add_argument("--public", help="optional public reply on the comment itself")
    k.add_argument("--ai-brief", dest="ai_brief",
                   help="describe your business and what to say; switches this keyword to AI replies")
    k.add_argument("--followup", help="nudge text if they reply but never give an email")
    k.add_argument("--followup-after", dest="followup_after", type=int,
                   help="hours before the nudge (default 4)")
    k.set_defaults(fn=cmd_keyword)
    sub.add_parser("list", help="show automations").set_defaults(fn=cmd_list)
    t = sub.add_parser("test", help="fire a fake comment through the handler"); t.add_argument("text"); t.set_defaults(fn=cmd_test)
    sub.add_parser("leads", help="captured emails").set_defaults(fn=cmd_leads)
    lg = sub.add_parser("log", help="recent sends"); lg.add_argument("-n", type=int, default=15); lg.set_defaults(fn=cmd_log)
    sub.add_parser("stats", help="counts and rate headroom").set_defaults(fn=cmd_stats)
    pl = sub.add_parser("poll", help="watch your comments without webhooks (no App Review needed)")
    pl.add_argument("--interval", type=int, default=60, help="seconds between polls")
    pl.add_argument("--media", type=int, default=10, help="how many recent posts to scan")
    pl.add_argument("--once", action="store_true", help="one pass, then exit")
    pl.set_defaults(fn=cmd_poll)
    ins = sub.add_parser("install", help="run the poller in the background, from login, forever")
    ins.add_argument("--interval", type=int, default=60)
    ins.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="stop and remove the background poller").set_defaults(fn=cmd_uninstall)
    sub.add_parser("tick", help="send due follow-ups and retry undelivered leads")\
       .set_defaults(fn=cmd_tick)
    sub.add_parser("refresh", help="extend the 60-day token").set_defaults(fn=cmd_refresh)
    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)
    st = sub.add_parser("selftest", help="prove the whole local chain works, no Meta needed")
    st.add_argument("--url", default="http://localhost:8000")
    st.set_defaults(fn=cmd_selftest)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
