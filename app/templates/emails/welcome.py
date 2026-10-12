"""Welcome email — tier-aware, sent after Stripe payment lands.

Renders the HTML + plaintext version of the welcome email customers
receive immediately after their checkout completes. HTML uses
table-based layout for Outlook compatibility and inlines all styles
because most clients strip <style> blocks.

Variables expected:
  - company_name: company they entered at signup
  - tier: "scout" | "growth" — drives the credit count + per-tier paragraph
  - cycle: "monthly" | "annual" — affects the billing-frequency line
  - magic_link: Supabase password-setup URL (from admin.generate_link)
  - cal_url: unused since 2026-07-08 (Solo is self-serve, no onboarding
    call); kept in the signature so callers don't need to change
  - dashboard_url: top-level dashboard URL (used in plaintext + footer)
"""

from __future__ import annotations

from html import escape as html_escape


# Per-tier copy block. Add Enterprise here once self-serve signup
# accepts it (currently Enterprise is call-only so this map is
# Scout/Growth-only).
TIER_CONFIG = {
    "starter": {
        "display": "Starter",
        # Caps-sold like Individual; the upgrade nudge names what Starter
        # lacks (smart groups) without apologising for it.
        "summary": "75 accounts a month · 15 watched · 200 contact unlocks a month",
        "lede": (
            "Welcome to Vera. Your 7-day free trial of the Starter plan has "
            "started and everything is switched on: research up to 75 accounts "
            "a month, Vera watches 15 of them weekly, and 200 contact unlocks a "
            "month. You won't be charged during your trial. After 7 days your "
            "plan starts, and you can cancel any time from Settings."
        ),
        "what_to_expect": [
            "Paste any company URL to research your first account in about 60 seconds",
            "Pick the accounts worth watching, Vera tracks them every week",
            "Unlock the contacts who matter and see what's changing around them",
            "When you want smart groups or more capacity, upgrade to Individual from Settings",
        ],
    },
    "solo": {
        # Marketed as "Individual" since 2026-07-14; the engine plan key
        # stays "solo". Sold on caps, never credits — keep the credit word
        # out. Sets its password on the /welcome page during signup, so this
        # email is a pure welcome — no password setup, no sign-in button.
        "display": "Individual",
        "summary": "150 accounts a month · 25 watched · 500 contact unlocks a month",
        "lede": (
            "Welcome to Vera. Your 7-day free trial of Individual has started "
            "and everything is switched on: research up to 150 accounts a month, "
            "Vera watches 25 of them weekly, and 500 contact unlocks a month. "
            "You won't be charged during your trial. After 7 days your plan "
            "starts, and you can cancel any time from Settings."
        ),
        "what_to_expect": [
            "Paste any company URL to research your first account in about 60 seconds",
            "Pick the accounts worth watching, Vera tracks them every week",
            "Unlock the contacts who matter and see what's changing around them",
        ],
    },
    "scout": {
        "display": "Scout",
        "credits": 100,
        "summary": "100 enrichment credits / month",
        "lede": (
            "You're on Scout — 100 enrichment credits a month, the REST "
            "API + real-time webhooks included, and your custom buyer "
            "profile already configured. Sign in and you can fire your "
            "first brief in under five minutes."
        ),
        "what_to_expect": [
            "Set your password using the link below — takes 10 seconds",
            "Configure or refine your buyer profile in Settings",
            "Drop in your first LinkedIn URL or let Vera surface suggestions",
            "Wire up the REST API + webhooks any time to push briefs into your CRM, Slack, or n8n",
        ],
    },
    "growth": {
        "display": "Growth",
        "credits": 300,
        "summary": "300 enrichment credits / month",
        "lede": (
            "You're on Growth — 300 enrichment credits a month, REST API + "
            "real-time webhooks, and up to five buyer profiles. Wire Vera "
            "into your CRM, Slack, or n8n whenever you're ready."
        ),
        "what_to_expect": [
            "Set your password using the link below",
            "Configure your buyer profiles in Settings — Growth includes 5",
            "Wire up the REST API + webhooks if you want briefs pushed into your CRM or Slack",
            "Drop your first LinkedIn URLs in — briefs in seconds",
        ],
    },
}

DEFAULT_TIER = "scout"

# Vera brand colours — the purple from the 2026-07 rebrand (marketing site
# brand #7c3aed) and standard light-mode email-friendly tones.
ACCENT = "#7c3aed"
TEXT_PRIMARY = "#0f172a"
TEXT_SECONDARY = "#475569"
BG_PANEL = "#f8fafc"
BG_BODY = "#ffffff"
BORDER = "#e2e8f0"


def _resolve_tier(tier: str) -> dict:
    return TIER_CONFIG.get((tier or "").lower()) or TIER_CONFIG[DEFAULT_TIER]


def _greeting_name(contact_name: str, company_name: str) -> str:
    """Prefer the person's first name; fall back to the company name."""
    first = (contact_name or "").strip().split(" ")[0] if contact_name else ""
    return first or company_name


def build_welcome_html(
    *,
    company_name: str,
    tier: str,
    cycle: str,
    magic_link: str,
    cal_url: str = "",  # kept for signature compat; Solo is self-serve, no call CTA
    dashboard_url: str,
    contact_name: str = "",
    needs_password: bool = True,
) -> str:
    cfg = _resolve_tier(tier)
    is_solo = (tier or "").lower() == "solo"
    # Escape: company_name / contact_name come straight from the signup
    # form and land inside HTML. Everything else here is our own copy.
    greeting_name = html_escape(_greeting_name(contact_name, company_name))
    # Self-serve caps tiers start on a 7-day free trial, so the plan card
    # shouldn't say "billed monthly" yet — the lede covers the billing.
    if (tier or "").lower() in ("starter", "solo"):
        cycle_label = "7-day free trial"
    else:
        cycle_label = "billed monthly" if (cycle or "").lower() != "annual" else "billed annually"
    bullets_html = "".join(
        f'<li style="margin:0 0 10px 0;font-size:15px;line-height:1.5;color:{TEXT_SECONDARY};">{step}</li>'
        for step in cfg["what_to_expect"]
    )

    # Primary CTA is driven by whether a password is set yet — NOT by tier.
    # Solo customers normally set theirs on the /welcome page at signup, so
    # the common Solo email shows a plain "sign in" link. But if they closed
    # that tab (or the webhook beat them to it), they still need the
    # magic-link "set your password" button to get in. needs_password also
    # covers teammate invites, who never have a password yet.
    if needs_password:
        password_cta_html = f"""
          <tr>
            <td style="padding:32px 40px 0 40px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="background:{ACCENT};border-radius:9999px;">
                    <a href="{magic_link}" style="display:inline-block;padding:14px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;letter-spacing:-0.005em;">
                      Set your password and sign in →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""
    else:
        # Password already set (Solo's happy path) — just point them in.
        password_cta_html = f"""
          <tr>
            <td style="padding:32px 40px 0 40px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="background:{ACCENT};border-radius:9999px;">
                    <a href="{dashboard_url}/login" style="display:inline-block;padding:14px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;letter-spacing:-0.005em;">
                      Sign in to your dashboard →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""

    # Secondary panel. Solo is fully self-serve (2026-07-08 — no
    # onboarding calls): point at the in-app setup instead of a call.
    # Other tiers get no secondary panel.
    if (tier or "").lower() == "solo":
        secondary_cta_html = f"""
          <tr>
            <td style="padding:32px 40px 0 40px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{BG_PANEL};border:1px solid {BORDER};border-radius:12px;padding:22px;">
                <tr>
                  <td>
                    <div style="font-size:16px;font-weight:600;color:{TEXT_PRIMARY};letter-spacing:-0.005em;">
                      Your setup takes about three minutes
                    </div>
                    <div style="margin-top:6px;font-size:14px;line-height:1.55;color:{TEXT_SECONDARY};">
                      Sign in and Vera configures itself around you. It reads
                      your website, builds your targeting, and finds your
                      first accounts automatically. Questions at any point?
                      Just reply to this email.
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""
    else:
        secondary_cta_html = ""

    # Gentle nudge in the opening so a customer who hasn't set a password
    # yet knows the button below is for exactly that. Hidden once they
    # have (they'd see the "sign in" CTA instead).
    if needs_password:
        intro_note_html = f"""
          <tr>
            <td style="padding:16px 40px 0 40px;">
              <p style="margin:0;font-size:15px;line-height:1.55;color:{TEXT_SECONDARY};">
                If you haven&rsquo;t already, set your password below and sign in — then you&rsquo;re straight into your dashboard.
              </p>
            </td>
          </tr>"""
    else:
        intro_note_html = ""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome to Vera</title>
</head>
<body style="margin:0;padding:0;background:{BG_PANEL};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{TEXT_PRIMARY};">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{BG_PANEL};padding:48px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="560" cellspacing="0" cellpadding="0" border="0" style="max-width:560px;background:{BG_BODY};border:1px solid {BORDER};border-radius:16px;overflow:hidden;">
          <!-- Header -->
          <tr>
            <td style="padding:40px 40px 8px 40px;">
              <div style="font-family:'SF Pro Display',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:{ACCENT};">Vera</div>
            </td>
          </tr>

          <!-- Heading -->
          <tr>
            <td style="padding:24px 40px 0 40px;">
              <h1 style="margin:0;font-size:28px;line-height:1.2;font-weight:600;color:{TEXT_PRIMARY};letter-spacing:-0.01em;">
                Welcome to Vera, {greeting_name}.
              </h1>
            </td>
          </tr>

          <!-- Lede -->
          <tr>
            <td style="padding:18px 40px 0 40px;">
              <p style="margin:0;font-size:16px;line-height:1.55;color:{TEXT_SECONDARY};">
                {cfg["lede"]}
              </p>
            </td>
          </tr>

          <!-- Opening nudge to set a password, when one isn't set yet -->
          {intro_note_html}

          <!-- CTA — set-password (no password yet) or sign-in (already set) -->
          {password_cta_html}

          <!-- Plan summary card -->
          <tr>
            <td style="padding:36px 40px 0 40px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{BG_PANEL};border:1px solid {BORDER};border-radius:12px;padding:20px 22px;">
                <tr>
                  <td>
                    <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:{TEXT_SECONDARY};">Your plan</div>
                    <div style="margin-top:6px;font-size:18px;font-weight:600;color:{TEXT_PRIMARY};letter-spacing:-0.01em;">
                      Vera {cfg["display"]} · {cycle_label}
                    </div>
                    <div style="margin-top:4px;font-size:13.5px;color:{TEXT_SECONDARY};">
                      {cfg["summary"]}
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- What to expect -->
          <tr>
            <td style="padding:36px 40px 0 40px;">
              <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:{TEXT_SECONDARY};">What's next</div>
              <ul style="margin:14px 0 0 0;padding-left:20px;">
                {bullets_html}
              </ul>
            </td>
          </tr>

          <!-- Secondary CTA — in-app setup (Solo) -->
          {secondary_cta_html}

          <!-- Sign-off -->
          <tr>
            <td style="padding:40px 40px 8px 40px;">
              <p style="margin:0;font-size:15px;line-height:1.55;color:{TEXT_SECONDARY};">
                Anything weird, any questions, any custom setup needs — just reply to this email.
              </p>
              <p style="margin:16px 0 0 0;font-size:15px;color:{TEXT_PRIMARY};">
                — The Vera team
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:40px 40px 32px 40px;border-top:1px solid {BORDER};">
              <div style="font-size:11.5px;color:{TEXT_SECONDARY};line-height:1.6;">
                Vera · Sales intelligence for cybersecurity teams<br>
                <a href="{dashboard_url}" style="color:{TEXT_SECONDARY};text-decoration:underline;">{dashboard_url}</a>
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def build_welcome_text(
    *,
    company_name: str,
    tier: str,
    magic_link: str,
    cal_url: str = "",  # kept for signature compat; unused
    contact_name: str = "",
    needs_password: bool = True,
    dashboard_url: str = "",
) -> str:
    """Plaintext fallback for clients that can't render HTML, and for
    deliverability scores (Gmail bumps senders that include text/plain).
    """
    cfg = _resolve_tier(tier)
    is_solo = (tier or "").lower() == "solo"
    greeting_name = _greeting_name(contact_name, company_name)
    bullets = "\n".join(f"  - {step}" for step in cfg["what_to_expect"])
    if is_solo:
        secondary = (
            "Your setup takes about three minutes. Sign in and Vera "
            "configures itself around you: it reads your website, builds "
            "your targeting, and finds your first accounts automatically. "
            "Questions at any point? Just reply to this email.\n\n"
        )
    else:
        secondary = ""
    # Show the set-password link when one is still needed; otherwise point
    # them at sign-in (they already set it on /welcome).
    if needs_password:
        password_line = (
            "If you haven't already, set your password and sign in — then "
            f"you're straight into your dashboard:\n{magic_link}\n\n"
        )
    elif dashboard_url:
        password_line = f"Sign in to your dashboard:\n{dashboard_url}/login\n\n"
    else:
        password_line = ""
    return (
        f"Welcome to Vera, {greeting_name}.\n\n"
        f"{cfg['lede']}\n\n"
        f"{password_line}"
        f"Your plan: Vera {cfg['display']} — {cfg['summary']}\n\n"
        f"What's next:\n{bullets}\n\n"
        f"{secondary}"
        f"Anything weird or any questions — just reply to this email.\n\n"
        f"— The Vera team"
    )
