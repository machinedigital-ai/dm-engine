# Personal setup

Running this on your own Instagram account. No App Review, no Business
Verification, no company documents, ~35 minutes.

**The click-by-click guide, with a test after every stage, is here:**
docs/onboarding.html (open it in a browser)

This file is the short reference for once you're through it.

## Why polling, not webhooks

Meta's webhooks page says Advanced Access is required to receive `comments`
notifications - and Advanced Access needs Business Verification, which wants
company registration documents. Their App Review page separately says an app
serving only an account you own needs no review. Those statements contradict
each other and people report it working both ways.

Private replies do NOT need webhooks. `cli.py poll` reads your own comments
directly and reaches the same `comment_id`, needing only
`instagram_business_basic` + `instagram_business_manage_comments`. Meta allows a
private reply up to **7 days** after a comment, so polling once a minute loses
nothing. Webhooks remain an optional upgrade at the end of the guide.

## Commands

`.env` is read automatically - no exporting.

```bash
./.venv/bin/python cli.py connect <token>     # once
./.venv/bin/python cli.py keyword GUIDE --reply "..." --ask-email "..." --thanks "..."
./.venv/bin/python cli.py doctor              # config, token health, poller status
./.venv/bin/python cli.py selftest            # proves the local chain, no Meta needed
./.venv/bin/python cli.py poll --once         # one pass against your real comments
./.venv/bin/python cli.py install             # run it in the background, from login, forever
```

Day to day:

```bash
./.venv/bin/python cli.py leads      # captured emails
./.venv/bin/python cli.py log        # what went out, what failed and why
./.venv/bin/python cli.py stats      # headroom against Meta's 750/hour cap
./.venv/bin/python cli.py refresh    # monthly - tokens die at 60 days, silently
```

## Extras (all optional, all off by default)

**AI replies** - answers what people actually asked instead of a fixed link.
Put `ANTHROPIC_API_KEY=...` in `.env`, then:

```bash
./.venv/bin/python cli.py keyword GUIDE \
  --reply "Here you go: yoursite.com/guide" \
  --ai-brief "We sell handmade candles from Bristol. UK shipping is 3 days, GBP 4.50. No refunds after 30 days."
```

Without the key the canned `--reply` is used, so nothing breaks - it just stops
being clever. The canned reply is also the fallback whenever the model errors
or declines.

**Follow-ups** - one nudge to people who replied but never gave an email:

```bash
./.venv/bin/python cli.py keyword GUIDE --reply "..." --ask-email "..." \
  --followup "Still want that guide? Just send your email." --followup-after 4
```

Meta only allows a DM within 24h of *their* last message, so someone who never
replied at all can never be nudged. Those are marked `expired` with the reason
in `cli.py log`.

**Leads into your email tool** - set `LEAD_WEBHOOK_URL` in `.env` to a
Zapier / Make / n8n catch-hook and every lead is POSTed there as it lands, with
retries. It is saved locally first, so a broken endpoint never loses one.

## Things that will bite you

- **750 private replies/hour.** A post that pulls 5,000 comments takes ~7 hours
  to drain. Nothing is lost - it retries as the window clears. Check `stats`.
- **One reply per comment, ever.** Link and email ask ship in the same message;
  `cli.py keyword` prints the byte count against Meta's 1000-byte cap.
- **24-hour window.** After someone messages you, you have a day to reply.
- **60-day token.** `cli.py refresh` monthly. `doctor` shows days left.
- **Editing `.env`** needs a restart: `cli.py uninstall && cli.py install`.
