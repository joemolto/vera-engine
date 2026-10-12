# Data providers

Vera doesn't come with data. It gathers it from providers you sign up to yourself: you create the accounts, you get the API keys, you pay them directly, and their terms apply to you. Vera only uses a provider when its key is set in `.env`; leave a key empty and Vera skips that source.

## Required

| Provider | What Vera uses it for | Where to get a key | Setting |
|---|---|---|---|
| [Anthropic](https://console.anthropic.com) | Every AI step: classifying signals, the "why this account matters" summary, building your ideal customer profile, the chat assistant | Console → API keys | `ANTHROPIC_API_KEY` (engine and dashboard) |
| [Apollo](https://www.apollo.io) | Company profiles, people search and key contacts, account suggestions, job postings, company news (leadership and funding), security team size, revealing contact details | Settings → Integrations → API. API access needs a paid plan | `APOLLO_API_KEY` (engine and edge functions) |

## Optional

Each one switches on more signals. Add them in any order.

| Provider | What it adds | Where to get a key | Setting |
|---|---|---|---|
| [OpenRouter](https://openrouter.ai) | Deep company research (via Perplexity), cleaner domain matching, filtering junk from account suggestions | Keys page | `OPENROUTER_API_KEY` |
| [PredictLeads](https://predictleads.com) | Tech stack, job openings, news and funding events, similar companies | API section of your account | `PREDICTLEADS_API_KEY`, `PREDICTLEADS_API_TOKEN` |
| [Tavily](https://tavily.com) | News and breach search, the website scan during onboarding, finding key contacts on the web | Dashboard → API keys | `TAVILY_API_KEY` |
| [Apify](https://apify.com) | LinkedIn: activity from the people you watch, job changes, company posts, competitor watch. Runs public Apify actors on your account | Settings → API & integrations | `APIFY_API_KEY` |
| [Findymail](https://www.findymail.com) | Finds email addresses when you unlock a contact and Apollo has none | API settings | `FINDYMAIL_API_KEY` |
| [LeakRadar](https://leakradar.io) | Dark-web credential exposure for an account's domain | Account → API | `LEAK_RADAR_API_KEY` |
| [Companies House](https://developer.company-information.service.gov.uk) | UK company revenue (free) | Register an application | `COMPANIES_HOUSE_API_KEY` |
| [Resend](https://resend.com) | Sends the daily digest and operator alerts | API keys, plus a verified sending domain | `RESEND_API_KEY`, `RESEND_FROM_EMAIL` |
| [logo.dev](https://logo.dev) | Sharper company logos in the dashboard (otherwise site favicons) | Dashboard → API keys | `NEXT_PUBLIC_LOGO_DEV_TOKEN` (dashboard) |

## Free sources (no key)

Vera also reads public sources with no account needed: company websites (via the Jina reader), ransomware leak-site listings (RansomLook), certificate logs (crt.sh), public job boards (Greenhouse, Lever, Ashby), Google News, and regulator news.

## Using a different provider

Every provider lives in its own module under `app/services/` (most data sources under `app/services/enrichers/`), reads its key from `app/config.py`, and does nothing when the key is empty. So:

- **Adding a source** (for example, another breach feed or a firmographics API) means writing a module in that pattern, adding its key to `app/config.py` and `.env.example`, and calling it from the enrichment pipeline (`app/services/enricher.py`, `app/services/company_enricher.py`) or the monitor (`app/services/watchlist/monitor.py`). The signals it produces go through `app/services/signal_writer.py`, which handles types, deduplication and scoring.
- **Replacing Apollo** (for example, with ZoomInfo) is a bigger job. Apollo is the core data source, and its calls and response shapes are spread across company research, people search, suggestions and contact reveals. Search for `APOLLO_API_KEY` and `apollo` to find every call site; each needs an equivalent call to the new provider that returns the same fields.
- **Replacing an optional provider** is usually one module: keep its function signatures and return shape, swap the API calls inside.
