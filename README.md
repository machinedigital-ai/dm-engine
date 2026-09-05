# dm-engine

Instagram comment-to-DM automation you run yourself. Free, no contact caps, and it actually captures the email.

Someone comments **GUIDE** on your post. They get a DM with your link. It asks whether
they'd like it by email too. They reply with their address. It's in your list. You were asleep.

## Why this exists

ManyChat bills per "active contact", so a post that goes viral becomes a surprise invoice -
that is the number-one complaint in their own pricing FAQ. The flat-rate tools ($10-19/mo)
deliver a link and stop. [OpenReply](https://github.com/diwenne/openreply) is free and very
good, and does none of: capture the email, write the reply, follow up.

This does all three, on your own machine, for $0.

|                              | dm-engine     | OpenReply       | ManyChat              |
|------------------------------|---------------|-----------------|-----------------------|
| Price                        | $0            | $0              | $29-39/mo + overages  |
| Contact caps                 | none          | none            | 2,500 on Pro          |
| Captures the email in the DM | yes           | no              | yes                   |
| AI replies in your voice     | yes           | no              | Pro and up            |
| Follow-up nudge              | yes           | no              | yes                   |
| Needs Meta App Review        | no (it polls) | yes (webhooks)  | n/a                   |
| Tracked links, follow gate   | no            | yes             | yes                   |
| Multi-account / agencies     | no            | yes             | yes                   |

Honest gaps: no flow builder, no multi-account, no "welcome new followers" (Meta has no API
for it), no broadcasts (Meta's 24-hour rule). If you need those, use one of the others.

## What it does

| Feature | How |
|---|---|
| Comment keyword -> DM with your link | `cli.py keyword GUIDE --reply "..."` |
| Capture the email inside the DM | add `--ask-email "..." --thanks "..."` |
| Several keywords, different links | run `cli.py keyword` again |
| AI writes the reply from a brief about your business | add `--ai-brief "..."` (needs `ANTHROPIC_API_KEY`; canned reply is the fallback) |
| Nudge people who replied but never gave an email | add `--followup "..." --followup-after 4` |
| Push every lead to Zapier / Make / n8n / your endpoint | set `LEAD_WEBHOOK_URL` |
| Answer DMs, not just comments | on by default, same keywords |
| Run in the background from login | `cli.py install` (macOS LaunchAgent) |

A commenter's words are treated as data, never as instructions: the AI is told not to invent
prices, promise refunds, or follow instructions embedded in a comment, and falls back to your
canned reply whenever the model is unavailable or declines.

## Quick start (your own account, ~35 minutes, no App Review)

Open **[docs/onboarding.html](docs/onboarding.html)** in a browser. Nine stages, a test after
each one, written for someone who has never seen Meta's developer dashboard.

The short version:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env            # then paste your Instagram app secret; .env is read automatically
./.venv/bin/python cli.py connect <token>     # token from Meta dashboard > Instagram > Generate token
./.venv/bin/python cli.py keyword GUIDE --reply "Here you go: yoursite.com/guide" \
    --ask-email "Want it by email too? Drop your address." --thanks "Sent."
./.venv/bin/python cli.py selftest            # proves the whole chain locally, no Meta needed
./.venv/bin/python cli.py poll --once         # reads your real comments (dry run until META_DRY_RUN=0)
./.venv/bin/python cli.py install             # runs it from login, forever
```

Day to day: `leads`, `log`, `stats`, `doctor`, and `refresh` once a month (Meta tokens die at 60 days).

## Why polling instead of webhooks

Meta's docs say the `comments` webhook needs Advanced Access, which needs Business Verification,
which wants company registration documents. Their App Review page separately says an app
serving only an account you own needs no review. Those statements contradict each other.

Private replies don't need a webhook. Polling your own recent posts reaches the same
`comment_id` with only `instagram_business_basic` + `instagram_business_manage_comments`, and
Meta allows a private reply up to 7 days after a comment, so a 60-second poll loses nothing.
Webhooks are supported too (`uvicorn app:app`) if you have the access.

## Meta limits the code enforces

| Limit | Where |
|---|---|
| One private reply per comment, ever | link + email ask ship as a single message |
| Private reply within 7 days of the comment | `COMMENT_WINDOW` |
| 750 private replies/hour per account | `_rate_ok`; blocked attempts are logged and retried |
| DM only within 24h of their last message | `send_dm` |
| 1000-byte message cap (bytes, not characters) | `fit()`, applied inside both send paths |
| Webhook redelivery | `seen_events` dedupe, released on retryable failure |
| `X-Hub-Signature-256` | `valid_signature`, no bypass in any mode |

## Tests

```bash
for t in test_app test_poll test_features test_fixes; do ./.venv/bin/python $t.py; done
```

93 checks. No Meta account needed - `META_DRY_RUN=1` records every outbound send to the
`sends` table instead of calling Meta. The suites cover signature verification, dedupe, the
750/hour cap, the 24-hour window, byte caps, AI fallback and prompt-injection framing,
follow-up scheduling, lead delivery with backoff, and every defect a 30-agent adversarial
review found (including a write-lock bug that silently dropped a captured lead).

## Status

Built for a single operator's own account. Multi-account and billing were deliberately left
out. If you'd pay for a hosted version so you never touch a terminal, say so in an issue -
that decides whether one gets built.

## License

MIT. Copyright (c) 2026 Machine Digital Inc.
