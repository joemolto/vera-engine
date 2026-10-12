# Setting up Vera

Vera has three parts. This guide sets up all of them, in this order:

1. **Database**: a Supabase project (Postgres, logins, file storage).
2. **Engine**: this repo, a Python API that researches and monitors accounts.
3. **Dashboard**: [vera-dashboard](https://github.com/joemolto/vera-dashboard), the web app, plus four small Supabase edge functions that sit between the browser and the engine.

Allow about an hour the first time.

## What you need

- A [Supabase](https://supabase.com) project (the free tier works).
- A server or machine for the engine: Python 3.13, reachable over HTTPS.
- Somewhere to run the dashboard: Node 22 on any host, or Vercel.
- The [Supabase CLI](https://supabase.com/docs/guides/cli), to deploy the edge functions.
- API keys you pay for yourself:
  - **Anthropic** (required): every AI step.
  - **Apollo** (required): company and people search. API access needs a paid Apollo plan.
  - Optional providers, each switching on more signals: OpenRouter, PredictLeads, Tavily, Apify, Findymail, LeakRadar, Companies House. Leave a key empty and Vera skips that source.

  [PROVIDERS.md](PROVIDERS.md) lists every provider: what it adds, where to get a key, and how to plug in a different one.

## 1. Database

In your Supabase project, open the SQL editor and run, in order:

1. `migrations/001_baseline.sql`: the whole schema.
2. `migrations/002_require_mfa.sql`: two-factor enforcement on org data.

The editor warns that the scripts contain destructive operations (they define functions that delete data when called, and revoke permissions). That's expected; run them.

Then, under **Authentication → URL Configuration**, set the Site URL to your dashboard's address (for example `https://vera.example.com`) and add `https://vera.example.com/reset-password` to the redirect URLs. Two-factor (TOTP) is on by default in Supabase; leave it on.

Note these values from **Project Settings → API Keys**: the project URL, the publishable key and the secret key. From **Project Settings → JWT Keys**, note the legacy JWT secret: the MCP server needs it, and refuses requests without it.

## 2. Engine

```bash
git clone https://github.com/joemolto/vera-engine.git
cd vera-engine
python3.13 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

- `SUPABASE_URL` and `SUPABASE_KEY` (the **secret** key).
- `ANTHROPIC_API_KEY` and `APOLLO_API_KEY`.
- `API_SECRET_KEY`: generate one with `openssl rand -hex 32`. The engine refuses to start with anything shorter than 32 characters.
- `DASHBOARD_URL`: your dashboard's address, so links in emails point there.
- Leave `SELF_HOSTED=true`. It removes every plan limit and starts the built-in scheduler.
- Optional: `RESEND_API_KEY` and `RESEND_FROM_EMAIL` to send the daily digest email, `OPERATOR_EMAIL` to get an alert if the daily monitor stops running, and any optional provider keys.

Start it:

```bash
./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Use exactly one worker: the scheduler runs inside each worker process, so more workers would run the daily jobs more than once.

Check it: `curl http://127.0.0.1:8000/api/v1/health` should report `"database": "connected"`.

### Put it behind HTTPS

The edge functions run in Supabase's cloud and call the engine with your `API_SECRET_KEY`, so the engine needs a public HTTPS address. Any reverse proxy works. With [Caddy](https://caddyserver.com), which gets the certificate for you:

```
engine.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Keep the engine bound to `127.0.0.1` so it's only reachable through the proxy. To keep it running, use a process manager such as systemd.

## 3. Your login

With `SELF_HOSTED=true` in `.env`:

```bash
PYTHONPATH=. ./venv/bin/python scripts/create_org.py --email you@company.com --org "Your Company" --domain company.com
```

It asks for a password, then creates your login and your organisation. Run it again with another email to add more logins (each gets its own organisation).

## 4. Edge functions

From a clone of [vera-dashboard](https://github.com/joemolto/vera-dashboard):

```bash
supabase login
supabase link --project-ref <your-project-ref>
supabase secrets set ENGINE_API_BASE=https://engine.example.com ENGINE_API_KEY=<your API_SECRET_KEY> APOLLO_API_KEY=<your Apollo key>
supabase functions deploy clara-api --no-verify-jwt
supabase functions deploy quick-enrich --no-verify-jwt
supabase functions deploy apollo-people
supabase functions deploy apollo-suggest-accounts
```

`clara-api` and `quick-enrich` check the signed-in user themselves (including the two-factor code), so Supabase's own check is switched off for them.

## 5. Dashboard

```bash
cd vera-dashboard
npm ci
cp .env.example .env.local
```

Fill in `.env.local`. The comments in the file explain each value; the essentials are the Supabase URL, publishable key, secret key and legacy JWT secret, `ENGINE_API_BASE` (your engine's HTTPS address), `ENGINE_API_KEY` (your `API_SECRET_KEY`), `ANTHROPIC_API_KEY` and `NEXT_PUBLIC_APP_URL`.

```bash
npm run build
npm start
```

Values starting `NEXT_PUBLIC_` are baked in at build time, so rebuild after changing them. On Vercel, import the repo and add the same values as environment variables.

## 6. First run

1. Open the dashboard and sign in with the login from step 3.
2. Set up two-factor with an authenticator app when asked.
3. Answer the onboarding questions. Vera builds your ideal customer profile from them and suggests first accounts.
4. Add accounts. Each one is researched straight away. Switch on **watching** for the ones you want monitored: the daily monitor (at `MONITOR_HOUR_UTC`, 04:00 UTC by default) re-checks each watched account once a week for new signals, and sends the digest.

## Keeping an eye on it

- `GET /api/v1/health` reports the database connection and `last_monitor_beat`, the last time the daily monitor finished. Point an uptime checker at it: the engine can't alert you if it's down itself.
- With `OPERATOR_EMAIL` and Resend set, the engine emails you if the daily monitor hasn't run for 27 hours.
- To run the monitor now instead of waiting: `curl -X POST https://engine.example.com/api/v1/watchlist/monitor-solo -H "X-API-Key: <your API_SECRET_KEY>"`.

## Moving an organisation between installs

`scripts/export_org.py` copies one organisation (accounts, contacts, signals, alerts, markets, groups, chat history, and the company profiles behind its accounts) out of a Vera database into a folder of JSON files. `scripts/import_org.py` loads that folder into another install, keeping every record linked.

```bash
# with .env pointing at the old database
PYTHONPATH=. ./venv/bin/python scripts/export_org.py --org-id <org uuid> --out ./org-export
# with .env pointing at the new, freshly migrated database
PYTHONPATH=. ./venv/bin/python scripts/import_org.py --in ./org-export
```

Logins can't be copied between Supabase projects, so each member gets a new login with a one-time password (printed at the end) and sets up their authenticator app again. API and MCP keys are re-created from the dashboard. The export folder holds personal data: keep it private and delete it after the import.

## Optional recurring jobs

The built-in scheduler runs the monitor, the digest and webhook delivery. These jobs keep other parts fresh; run them from cron (or any scheduler) if you use those parts. Each call needs your `API_SECRET_KEY` and may spend provider credits.

```
# Daily: match public threat feeds (NCSC, NVD, CISA KEV, ICO) to your accounts
0 6 * * *  curl -s -X POST https://engine.example.com/api/v1/threats/monitor -H "X-API-Key: $KEY"
# Weekly: refresh each account's "why this matters" summary
0 5 * * 1  curl -s -X POST https://engine.example.com/api/v1/accounts/why-this-matters/refresh-solo -H "X-API-Key: $KEY"
# Weekly: recompute the market page
0 5 * * 2  curl -s -X POST https://engine.example.com/api/v1/accounts/market/refresh-all -H "X-API-Key: $KEY"
# Weekly: competitor watch (needs an Apify key)
0 5 * * 3  curl -s -X POST https://engine.example.com/api/v1/competitors/scan -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"all": true}'
# Monthly: re-verify company profiles (revenue, size, stack)
0 3 1 * *  curl -s -X POST https://engine.example.com/api/v1/watchlist/enrich-sweep -H "X-API-Key: $KEY"
```

## Troubleshooting

- **"Invalid API key" from the engine at start-up**: `SUPABASE_KEY` must be the secret key, not the publishable one.
- **Dashboard actions fail with 500 errors**: `ENGINE_API_BASE` isn't set, or the dashboard can't reach the engine.
- **Account research never finishes**: check the edge function secrets, and that `ENGINE_API_BASE` there is the public HTTPS address, not localhost.
- **Sign-in loops or reset emails link to the wrong site**: check the Site URL and redirect URLs in Supabase Authentication settings.
- **A whole kind of signal never appears**: the provider key behind it is empty. See `.env.example`.
- **The MCP server answers "temporarily unable to authorize"**: `SUPABASE_JWT_SECRET` isn't set in the dashboard.
- **An added account gets no daily signals**: adding an account researches it once; switch on watching for it in the dashboard so the daily monitor includes it.
