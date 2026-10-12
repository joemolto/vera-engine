# Vera

Vera is sales intelligence for cybersecurity vendors. You tell it who you sell to; it watches those accounts and the people in them, and tells you when something changes that makes a conversation worth having: a new security leader, security hiring, funding, a breach in the news, leaked credentials, a change in their public security posture.

This repo is the **engine**: the Python API that researches accounts, collects and scores signals, runs the daily monitor and sends the digest. The web app lives in [vera-dashboard](https://github.com/joemolto/vera-dashboard).

## Background

Vera ran as a hosted product in 2026. It has been wound down, and the code is published here so anyone can run it themselves with their own data-provider keys. The project is complete and not actively maintained: issues and pull requests aren't monitored.

## What it does

- **Account research**: firmographics, revenue, tech stack, security team shape, public security posture (trust centre, bug bounty, disclosure policy, email security) and key contacts.
- **Monitoring**: a daily pass that re-checks each watched account once a week for new signals: leadership changes, security and GRC hiring, funding and acquisitions, breach mentions, dark-web credential exposure, posture changes, and activity from the people you watch.
- **Scoring**: signals grouped by risk, posture, maturity and commercial impact, weighted by your own priorities and decayed by age.
- **Suggestions**: new accounts that match your ideal customer profile.
- **Delivery**: a daily digest email, an AI chat over your data, an MCP server for AI assistants, a REST API and signed webhooks.

## How it fits together

```
browser ── dashboard (Next.js) ──┬── Supabase (Postgres, auth, row-level security)
                                 └── edge functions ── engine (this repo) ── data providers
                                                          │
                                                          └── built-in scheduler (daily monitor, digest, webhooks)
```

- `app/routers/`: HTTP endpoints. Internal routes take the `X-API-Key` admin key; the public API takes per-org `vera_live_` keys.
- `app/services/`: enrichment, signal writing, scoring, monitoring, digest and email.
- `app/services/scheduler.py`: the daily monitor, a health check and webhook delivery, running inside the engine process.
- `migrations/`: the database schema (`001_baseline.sql`, then `002_require_mfa.sql`).
- `scripts/create_org.py`: creates your first login and organisation.

## Hosted-era code

The hosted service's billing (Stripe checkout, plans, credits, trials) and its signup and trial emails are still in the code, so the history of how Vera worked is intact. With `SELF_HOSTED=true` none of it runs: there are no plans or limits, and billing routes return 404.

## Running it

See [SETUP.md](SETUP.md). You'll need a Supabase project, an Anthropic key and a paid Apollo plan with API access; other providers are optional and each one switches on more signals. [PROVIDERS.md](PROVIDERS.md) covers every provider, where to get its key, and how to use a different one.

## Tests

```bash
./venv/bin/python -m pytest tests -q
```

## Licence

MIT. See [LICENSE](LICENSE). Data from third-party providers is subject to those providers' own terms, which you agree to with your own accounts.
