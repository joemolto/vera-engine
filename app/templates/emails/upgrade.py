"""Upgrade email — sent when a Starter org moves to Individual.

Fired from the Stripe subscription webhook the moment the tier flip
lands in the DB (billing._persist_subscription), so it only ever sends
on a real, paid upgrade. Same table-based layout, colours and voice as
the welcome email — import its constants so the two can't drift.
"""

from __future__ import annotations

from html import escape as html_escape

from app.templates.emails.welcome import (
    ACCENT,
    BG_BODY,
    BG_PANEL,
    BORDER,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    _greeting_name,
)

SUBJECT = "Thanks for upgrading. You're on Vera Individual."

_SUMMARY = "150 accounts a month · 25 watched · 500 contact unlocks a month"

_LEDE = (
    "Thanks for upgrading. You're on Vera Individual now, and everything "
    "below is already switched on."
)

_UNLOCKED = [
    "Smart groups: group your accounts by hand or by rule, and Vera keeps them up to date",
    "Research up to 150 accounts a month, up from 75",
    "Vera watches 25 accounts weekly, up from 15",
    "500 contact unlocks a month, up from 200",
    "Watch up to 25 people, up from 10",
    "Need more? Capacity packs are in Settings, prorated to your billing cycle",
]


def build_upgrade_html(
    *,
    company_name: str,
    dashboard_url: str,
    contact_name: str = "",
) -> str:
    # Escape user-supplied name before it lands in HTML (plaintext builder
    # below needs no escaping).
    greeting_name = html_escape(_greeting_name(contact_name, company_name))
    bullets_html = "".join(
        f'<li style="margin:0 0 10px 0;font-size:15px;line-height:1.5;color:{TEXT_SECONDARY};">{step}</li>'
        for step in _UNLOCKED
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Thanks for upgrading</title>
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
                Thanks for upgrading, {greeting_name}.
              </h1>
            </td>
          </tr>

          <!-- Lede -->
          <tr>
            <td style="padding:18px 40px 0 40px;">
              <p style="margin:0;font-size:16px;line-height:1.55;color:{TEXT_SECONDARY};">
                {_LEDE}
              </p>
            </td>
          </tr>

          <!-- CTA -->
          <tr>
            <td style="padding:32px 40px 0 40px;">
              <table role="presentation" cellspacing="0" cellpadding="0" border="0">
                <tr>
                  <td style="background:{ACCENT};border-radius:9999px;">
                    <a href="{dashboard_url}/dashboard" style="display:inline-block;padding:14px 28px;font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;letter-spacing:-0.005em;">
                      Open your dashboard →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Plan summary card -->
          <tr>
            <td style="padding:36px 40px 0 40px;">
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{BG_PANEL};border:1px solid {BORDER};border-radius:12px;padding:20px 22px;">
                <tr>
                  <td>
                    <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:{TEXT_SECONDARY};">Your plan</div>
                    <div style="margin-top:6px;font-size:18px;font-weight:600;color:{TEXT_PRIMARY};letter-spacing:-0.01em;">
                      Vera Individual · billed monthly
                    </div>
                    <div style="margin-top:4px;font-size:13.5px;color:{TEXT_SECONDARY};">
                      {_SUMMARY}
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Now unlocked -->
          <tr>
            <td style="padding:36px 40px 0 40px;">
              <div style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:600;letter-spacing:0.18em;text-transform:uppercase;color:{TEXT_SECONDARY};">Now unlocked</div>
              <ul style="margin:14px 0 0 0;padding-left:20px;">
                {bullets_html}
              </ul>
            </td>
          </tr>

          <!-- Sign-off -->
          <tr>
            <td style="padding:40px 40px 8px 40px;">
              <p style="margin:0;font-size:15px;line-height:1.55;color:{TEXT_SECONDARY};">
                Anything weird, any questions — just reply to this email.
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


def build_upgrade_text(
    *,
    company_name: str,
    dashboard_url: str,
    contact_name: str = "",
) -> str:
    """Plaintext fallback, same reasons as the welcome email."""
    greeting_name = _greeting_name(contact_name, company_name)
    bullets = "\n".join(f"  - {step}" for step in _UNLOCKED)
    return (
        f"Thanks for upgrading, {greeting_name}.\n\n"
        f"{_LEDE}\n\n"
        f"Open your dashboard:\n{dashboard_url}/dashboard\n\n"
        f"Your plan: Vera Individual — {_SUMMARY}\n\n"
        f"Now unlocked:\n{bullets}\n\n"
        f"Anything weird or any questions — just reply to this email.\n\n"
        f"— The Vera team"
    )
