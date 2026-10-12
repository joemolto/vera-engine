"""Founder follow-up email — a short personal note, sent ~10 minutes
after a Solo signup completes.

Deliberately plainer than the branded welcome email: no plan card, no
eyebrow wordmark, no feature bullets. It should read like a personal
email a person actually typed — short, warm, one clear ask (hit reply
if anything's unclear). Solo onboarding is fully self-serve as of
2026-07-08, so there is no setup-call push anywhere in it.

Variables expected:
  - contact_name: the person's name (we greet by first name)
  - company_name: fallback greeting if no contact name
  - cal_url: unused since 2026-07-08; kept for signature compat
"""

from __future__ import annotations


TEXT_PRIMARY = "#0f172a"
TEXT_SECONDARY = "#334155"
BG = "#ffffff"


def _first_name(contact_name: str, company_name: str) -> str:
    first = (contact_name or "").strip().split(" ")[0] if contact_name else ""
    return first or (company_name or "there")


SUBJECT = "Quick hello from the Vera team"


def build_founder_html(*, contact_name: str, company_name: str, cal_url: str) -> str:
    name = _first_name(contact_name, company_name)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hello from Vera</title>
</head>
<body style="margin:0;padding:0;background:{BG};font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:{TEXT_PRIMARY};">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{BG};padding:32px 16px;">
    <tr>
      <td align="center">
        <table role="presentation" width="540" cellspacing="0" cellpadding="0" border="0" style="max-width:540px;">
          <tr>
            <td style="font-size:16px;line-height:1.62;color:{TEXT_SECONDARY};">
              <p style="margin:0 0 18px 0;">Hey {name},</p>

              <p style="margin:0 0 18px 0;">
                Thank you for signing up to Vera's free trial. We're glad to
                have you on board and we really hope you like it and stick
                around.
              </p>

              <p style="margin:0 0 18px 0;">
                Most sales tools just don't give you the depth you need on
                cybersecurity accounts. They run the same tired playbook of high
                volume, low quality, never really understanding whether a business
                needs what you sell. Vera is built for low volume, high quality,
                and a depth of insight on cybersecurity accounts.
              </p>

              <p style="margin:0 0 22px 0;">
                Vera researches your accounts, watches them, and surfaces what's
                genuinely worth a conversation, so you spend your time building
                meaningful connections with the people and accounts you most want
                to win.
              </p>

              <p style="margin:0 0 22px 0;">
                Your setup takes about three minutes and Vera does the rest,
                so dive in. If anything is unclear, or you just want a second
                opinion on your targeting, hit reply. We read every one.
              </p>

              <p style="margin:0;color:{TEXT_PRIMARY};">
                Speak soon,<br>
                The Vera team
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def build_founder_text(*, contact_name: str, company_name: str, cal_url: str) -> str:
    name = _first_name(contact_name, company_name)
    return (
        f"Hey {name},\n\n"
        f"Thank you for signing up to Vera's free trial. We're glad to have you "
        f"on board and we really hope you like it and stick around.\n\n"
        f"Most sales tools just don't give you the depth you need on cybersecurity "
        f"accounts. They run the same tired playbook of high volume, low quality, "
        f"never really understanding whether a business needs what you sell. Vera "
        f"is built for low volume, high quality, and a depth of insight on "
        f"cybersecurity accounts.\n\n"
        f"Vera researches your accounts, watches them, and surfaces what's "
        f"genuinely worth a conversation, so you spend your time building "
        f"meaningful connections with the people and accounts you most want to "
        f"win.\n\n"
        f"Your setup takes about three minutes and Vera does the rest, so "
        f"dive in. If anything is unclear, or you just want a second opinion "
        f"on your targeting, hit reply. We read every one.\n\n"
        f"Speak soon,\nThe Vera team"
    )
