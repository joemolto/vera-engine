"""Resend-backed transactional email sending.

Bypasses Supabase Auth's built-in invite email so we control the full
HTML, can interpolate custom variables (company name, tier, cycle,
onboarding link), and can branch copy by tier.

Today: welcome email only. Add new templates as `send_<name>_email`
helpers — each one renders a `<name>.py` template under
`app/templates/emails/`, then calls `_send_via_resend` to deliver.
"""

from __future__ import annotations

import html

_esc = html.escape
import logging
import os
from typing import Any, Dict, Optional

import httpx

from app.config import settings
from app.templates.emails.welcome import build_welcome_html, build_welcome_text
from app.templates.emails.founder_followup import (
    SUBJECT as FOUNDER_SUBJECT,
    build_founder_html,
    build_founder_text,
)

logger = logging.getLogger(__name__)

_RESEND_API = "https://api.resend.com/emails"


def _send_via_resend(
    to_email: str,
    subject: str,
    html: str,
    text: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    from_name: Optional[str] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """POST to Resend's /emails endpoint. Raises on non-2xx so the caller
    knows the send failed (no silent swallowing — we want the webhook
    handler to log the error and Stripe to redeliver if it makes sense).

    scheduled_at: ISO-8601 timestamp (or Resend natural-language string).
        When set, Resend delivers the email at that time rather than now —
        used for the founder follow-up (~10 min after signup).
    from_name: overrides the default sender display name (the founder
        follow-up sends as "The Vera team"); the from address is unchanged
        so deliverability stays on the verified domain.
    """
    if not settings.RESEND_API_KEY or not settings.RESEND_FROM_EMAIL:
        raise RuntimeError(
            "RESEND_API_KEY or RESEND_FROM_EMAIL not set. Configure both in "
            ".env before any email send is attempted."
        )

    sender_name = from_name or settings.RESEND_FROM_NAME
    payload: Dict[str, Any] = {
        "from": f"{sender_name} <{settings.RESEND_FROM_EMAIL}>",
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at
    if headers:
        payload["headers"] = headers

    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=15) as client:
        resp = client.post(_RESEND_API, json=payload, headers=headers)
        if not resp.is_success:
            logger.error(
                "Resend send failed (%d) to %s: %s",
                resp.status_code, to_email, resp.text[:300],
            )
            resp.raise_for_status()
        data = resp.json()

    logger.info("Resend send OK to %s — id=%s", to_email, data.get("id"))
    return data


def _add_to_resend_audience(email: str) -> None:
    """Best-effort: add a captured free-report email to a Resend Audience so it
    can be enrolled in a follow-up sequence. Gated on RESEND_AUDIENCE_ID; never
    raises — a failure here must not break the report send."""
    audience_id = os.getenv("RESEND_AUDIENCE_ID")
    if not audience_id or not settings.RESEND_API_KEY:
        return
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                f"https://api.resend.com/audiences/{audience_id}/contacts",
                json={"email": email, "unsubscribed": False},
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
        if resp.is_success:
            logger.info("Resend audience add OK: %s", email)
        else:
            logger.warning(
                "Resend audience add failed (%d): %s", resp.status_code, resp.text[:200]
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Resend audience add error for %s: %s", email, e)


def _pretty_size(band: Any) -> str:
    """Humanise an Apollo employee band ('1001,5000' -> '1,001–5,000 employees')."""
    if not band:
        return ""
    parts = [p.strip() for p in str(band).split(",")]

    def fmt(n: str) -> str:
        try:
            return f"{int(n):,}"
        except (ValueError, TypeError):
            return n

    if len(parts) == 2:
        lo, hi = parts
        return f"{fmt(lo)}+ employees" if not hi else f"{fmt(lo)}–{fmt(hi)} employees"
    return str(band)


# --- free-report email — light, editorial, facts-only (mirrors the site pop-up) -
# Company-level public intelligence ONLY; never a person. Outlook/MSO-safe.
# No editorial tails, no "what this means" glosses — the facts stand alone.

_EFONT = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
_HERO_PRIORITY = [
    "Funding", "Breach / incident reported", "Acquisition",
    "New partnership", "Expansion", "Leadership", "Product launch",
]

# Money and incidents still outrank the security-team line in a subject. The
# softer half does not: "Bodycote partners with Hgen" is precisely the
# press-release filler the free report was rewritten to stop leading with.
_HERO_ABOVE_SECURITY = 3


def _posture_strong(es: Dict[str, Any]) -> bool:
    return (
        bool(es.get("spf"))
        and bool(es.get("dmarc"))
        and es.get("dmarc_policy") in ("quarantine", "reject")
    )


def _build_free_report_subject(report: Dict[str, Any]) -> str:
    company = report.get("company") or {}
    name = (company.get("name") or report.get("domain") or "your company").strip()
    by_label: Dict[str, str] = {}
    for h in report.get("highlights") or []:
        if isinstance(h, dict) and h.get("headline"):
            by_label.setdefault(h.get("label") or "", h.get("headline") or "")
    es = report.get("email_security") or {}
    hard = next((l for l in _HERO_PRIORITY[:_HERO_ABOVE_SECURITY] if l in by_label), None)
    if hard:
        return f"{name}: {by_label[hard]}"[:78]
    # Compact form, not the full verdict sentence — at 78 chars the sentence
    # truncates mid-clause and reads as a bug.
    team = (report.get("security_overview") or {}).get("team") or {}
    band = team.get("expected_band") or []
    total = int(team.get("total") or 0)
    if team.get("verdict") and len(band) == 2:
        return f"{name}: {total} security staff vs {band[0]}-{band[1]} typical"[:78]
    # No baseline (unknown company size) — the unstaffed disciplines are still
    # a fact worth opening on, and beat falling through to firmographics.
    gaps = team.get("gaps") or []
    if total and gaps:
        return f"{name}: {total} in security, nobody in {gaps[0].lower()}"[:78]
    # Softer press-release categories only now get a look in.
    soft = next((l for l in _HERO_PRIORITY[_HERO_ABOVE_SECURITY:] if l in by_label), None)
    if soft:
        return f"{name}: {by_label[soft]}"[:78]
    if es and not _posture_strong(es):
        return f"{name}: DMARC not enforced"[:78]
    size = _pretty_size(company.get("size_band"))
    ind = company.get("industry") or "company"
    loc = company.get("hq_country")
    return f"{name}: {(size + ' ') if size else ''}{ind}{(', ' + loc) if loc else ''}"[:78]


def _build_free_report_html(report: Dict[str, Any]) -> str:
    """Light, editorial, facts-only report email — mirrors the on-site pop-up's
    restraint. Company-level public intelligence only; never a person. MSO-safe."""
    company = report.get("company") or {}
    domain = report.get("domain") or ""
    name = (company.get("name") or domain or "this company").strip()
    e = html.escape
    F = _EFONT

    es = report.get("email_security") or {}
    enforced = _posture_strong(es)
    highlights = [
        h for h in (report.get("highlights") or [])
        if isinstance(h, dict) and h.get("headline")
    ][:4]
    stack = [s.get("name") if isinstance(s, dict) else s for s in (report.get("security_stack") or [])]
    stack = [e(str(s).strip()) for s in stack if s][:8]

    ce = report.get("credential_exposure") or {}
    exposed = int(ce.get("employees") or 0)
    recent90 = int(ce.get("recent_90d") or 0)
    signals_count = int(report.get("signals_count") or len(highlights))

    # Present only on the deep (emailed) report; the on-site payload has none.
    so = report.get("security_overview") or {}

    desc = (company.get("description") or "").strip()
    # Trim to whole sentences so the blurb never cuts off mid-thought (a 155-char
    # hard cut produced lines like "...specializes in helping…").
    if len(desc) > 240:
        cut = desc[:240]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        desc = cut[: end + 1] if end >= 60 else cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    industry = e(company["industry"]) if company.get("industry") else ""

    PRI, SEC, MUT, LOCK, AMBER, HAIR = "#0a0a0a", "#555555", "#9b9b97", "#aeaea9", "#b45309", "#ededeb"
    sec = f"border-top:1px solid {HAIR};padding:22px 0 0 0;"
    lbl = f"font-family:{F};font-size:11.5px;color:{MUT};margin:0 0 12px 0;"

    desc_block = (
        f'<tr><td style="font-family:{F};font-size:13.5px;line-height:1.65;color:{SEC};padding:16px 0 0 0;">{e(desc)}</td></tr>'
        if desc else ""
    )

    # Firmographics
    facts = []
    size = _pretty_size(company.get("size_band"))
    if size:
        facts.append(("Size", e(size)))
    if company.get("hq_country"):
        facts.append(("HQ", e(company["hq_country"])))
    if company.get("founded_year"):
        facts.append(("Founded", e(str(company["founded_year"]))))
    facts_block = ""
    if facts:
        cells = "".join(
            f'<td valign="top" style="font-family:{F};padding:0 28px 0 0;">'
            f'<div style="font-size:11px;color:{MUT};">{l}</div>'
            f'<div style="font-size:14px;color:{PRI};padding-top:4px;">{v}</div></td>'
            for l, v in facts
        )
        facts_block = f'<tr><td style="{sec}"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>{cells}</tr></table></td></tr>'

    # Credential exposure — the standout finding (count only; detail in-product)
    exposure_block = ""
    if exposed > 0:
        recent_txt = f" &middot; {recent90} in the last 90 days" if recent90 else ""
        exposure_block = (
            f'<tr><td style="{sec}"><div style="{lbl}">Credential exposure</div>'
            f'<div style="font-family:{F};font-size:30px;font-weight:600;letter-spacing:-0.02em;color:{PRI};line-height:1;">{exposed:,}</div>'
            f'<div style="font-family:{F};font-size:14px;color:{SEC};padding-top:8px;">employee credentials found in known breaches{recent_txt}</div>'
            f'<div style="font-family:{F};font-size:12.5px;color:{MUT};padding-top:10px;">The exposed accounts &amp; sources &mdash; in Vera</div>'
            f"</td></tr>"
        )

    # Email security — plain line; a gap token reads amber
    posture_block = ""
    if es:
        dmarc_txt = (f"DMARC {e(es.get('dmarc_policy'))}" if enforced and es.get("dmarc_policy")
                     else "DMARC enforced" if enforced
                     else "DMARC not enforced" if es.get("dmarc") else "No DMARC")
        toks = [
            f'<span style="color:{PRI if es.get("spf") else AMBER};">{"SPF" if es.get("spf") else "No SPF"}</span>',
            f'<span style="color:{PRI if enforced else AMBER};">{dmarc_txt}</span>',
        ]
        if es.get("mx_provider"):
            toks.append(f'<span style="color:{SEC};">{e(es.get("mx_provider"))}</span>')
        posture_block = (
            f'<tr><td style="{sec}"><div style="{lbl}">Email security</div>'
            f'<div style="font-family:{F};font-size:13.5px;">{" &middot; ".join(toks)}</div></td></tr>'
        )

    # N buying signals — clean list, no bullets
    signals_block = ""
    if highlights:
        rows = "".join(
            f'<div style="font-family:{F};font-size:13.5px;line-height:1.55;margin:0 0 9px 0;">'
            f'<span style="color:{PRI};font-weight:600;">{e(h.get("label") or "Signal")}</span> '
            f'<span style="color:{SEC};">&mdash; {e(h.get("headline"))}</span></div>'
            for h in highlights
        )
        sig_label = (f"{signals_count} buying signal{'s' if signals_count != 1 else ''}"
                     if signals_count else "What&rsquo;s moving")
        more = max(0, signals_count - len(highlights))
        more_txt = (f'<div style="font-family:{F};font-size:13px;color:{MUT};padding-top:2px;">+ {more} more</div>'
                    if more else "")
        signals_block = f'<tr><td style="{sec}"><div style="{lbl}">{sig_label}</div>{rows}{more_txt}</td></tr>'

    # Security stack — names only
    stack_block = ""
    if stack:
        stack_block = (
            f'<tr><td style="{sec}"><div style="{lbl}">Security stack</div>'
            f'<div style="font-family:{F};font-size:13.5px;color:{SEC};">{" &middot; ".join(stack)}</div></td></tr>'
        )

    # Security team — headcount against the size+sector baseline. The verdict
    # sentence is the point; the bare number is trivia.
    team = so.get("team") or {}
    team_total = int(team.get("total") or 0)
    team_block = ""
    if team and (team.get("verdict") or team_total):
        areas = [a for a in (team.get("by_area") or []) if a.get("count")]
        area_txt = " &middot; ".join(
            f'{e(a["label"])} {int(a["count"])}' for a in areas
        )
        gaps = [e(g) for g in (team.get("gaps") or [])][:3]
        gap_txt = (
            f'<div style="font-family:{F};font-size:13px;color:{AMBER};padding-top:8px;">'
            f'No one found in: {", ".join(gaps)}</div>' if gaps else ""
        )
        # No verdict means we don't know the company's size, so no baseline
        # comparison may be drawn. The count and role mix are still observed.
        caption = (
            e(team["verdict"]) if team.get("verdict")
            else f"security-titled staff found at {e(domain)}"
        )
        team_block = (
            f'<tr><td style="{sec}"><div style="{lbl}">Security team</div>'
            f'<div style="font-family:{F};font-size:30px;font-weight:600;letter-spacing:-0.02em;color:{PRI};line-height:1;">{team_total}</div>'
            f'<div style="font-family:{F};font-size:14px;color:{SEC};padding-top:8px;">{caption}</div>'
            + (f'<div style="font-family:{F};font-size:13px;color:{MUT};padding-top:8px;">{area_txt}</div>' if area_txt else "")
            + gap_txt
            + "</td></tr>"
        )

    # Open security roles — evidence of intent, and a budget tell.
    hiring = so.get("hiring") or {}
    hiring_block = ""
    if int(hiring.get("open_security_roles") or 0) > 0:
        n = int(hiring["open_security_roles"])
        recent_n = int(hiring.get("recent_90d") or 0)
        recent_txt = f" &middot; {recent_n} posted in the last 90 days" if recent_n else ""
        rows = "".join(
            f'<div style="font-family:{F};font-size:13.5px;line-height:1.55;margin:0 0 7px 0;color:{SEC};">'
            f'{e(t.get("title") or "")}'
            + (f' <span style="color:{MUT};">&middot; {e(t.get("location"))}</span>' if t.get("location") else "")
            + "</div>"
            for t in (hiring.get("titles") or [])[:5]
        )
        hiring_block = (
            f'<tr><td style="{sec}"><div style="{lbl}">Hiring in security</div>'
            f'<div style="font-family:{F};font-size:14px;color:{PRI};padding-bottom:10px;">'
            f'{n} open security role{"s" if n != 1 else ""}{recent_txt}</div>{rows}</td></tr>'
        )

    # What they publish. The absence is as informative as the presence, so an
    # empty result renders as a finding rather than a missing section.
    posture_pages = so.get("posture") or {}
    pages_block = ""
    if posture_pages:
        pub = posture_pages.get("published") or []
        if pub:
            names = " &middot; ".join(e(p.get("label") or "") for p in pub)
            extra = " &middot; security.txt" if posture_pages.get("has_security_txt") else ""
            body = f'<div style="font-family:{F};font-size:13.5px;color:{SEC};">{names}{extra}</div>'
            missing = [e(m) for m in (posture_pages.get("missing") or [])][:3]
            if missing:
                body += (f'<div style="font-family:{F};font-size:13px;color:{MUT};padding-top:8px;">'
                         f'Not found: {", ".join(missing)}</div>')
        elif posture_pages.get("publishes_nothing"):
            body = (f'<div style="font-family:{F};font-size:13.5px;color:{AMBER};">'
                    "No security page, trust centre or security.txt found</div>")
        else:
            body = ""
        if body:
            pages_block = f'<tr><td style="{sec}"><div style="{lbl}">Security assurance</div>{body}</td></tr>'

    # Internet-facing estate. Unversioned kit is still worth naming — you
    # cannot make a CVE claim from it, but it is a real opener.
    perim = so.get("perimeter") or {}
    perimeter_block = ""
    if perim.get("scanned") and (perim.get("edge_kit") or perim.get("hostnames_published")):
        kit = (perim.get("edge_kit") or [])[:5]
        rows = "".join(
            f'<div style="font-family:{F};font-size:13.5px;line-height:1.55;margin:0 0 7px 0;">'
            f'<span style="color:{PRI};">{e(k.get("product") or "")}</span>'
            + (f' <span style="color:{SEC};">{e(k.get("version"))}</span>' if k.get("version")
               else f' <span style="color:{MUT};">version not disclosed</span>')
            + f' <span style="color:{MUT};">&middot; {e(k.get("host") or "")}</span></div>'
            for k in kit
        )
        published_n = int(perim.get("hostnames_published") or 0)
        head = (f'<div style="font-family:{F};font-size:14px;color:{PRI};padding-bottom:10px;">'
                f'{published_n} hostname{"s" if published_n != 1 else ""} published in '
                f"certificate transparency logs</div>") if published_n else ""
        if rows or head:
            perimeter_block = (
                f'<tr><td style="{sec}"><div style="{lbl}">Internet-facing estate</div>'
                f"{head}{rows}</td></tr>"
            )

    # In Vera (locked) — muted rows, no sell
    locked_items = ["Security leaders: CISO, VP Security"]
    if exposed == 0:
        locked_items.append("Breach &amp; credential exposure")
    locked_items.append("Continuous monitoring")
    locked_rows = "".join(
        f'<div style="font-family:{F};font-size:13.5px;color:{LOCK};margin:0 0 9px 0;">{r}</div>'
        for r in locked_items
    )
    locked_block = f'<tr><td style="{sec}"><div style="{lbl}">In Vera</div>{locked_rows}</td></tr>'

    # CTA — slim near-black button
    cta_url = f"{settings.DASHBOARD_URL}/dashboard/settings"
    cta_block = (
        f'<tr><td style="border-top:1px solid {HAIR};padding:26px 0 0 0;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td bgcolor="#0a0a0a" style="border-radius:8px;">'
        '<!--[if mso]>'
        f'<v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{cta_url}" style="height:44px;v-text-anchor:middle;width:300px;" arcsize="18%" stroke="f" fillcolor="#0a0a0a">'
        '<w:anchorlock/><center style="color:#ffffff;font-family:Segoe UI,Arial,sans-serif;font-size:14px;font-weight:bold;">See it all in Vera &rarr;</center>'
        '</v:roundrect><![endif]-->'
        '<!--[if !mso]><!-- -->'
        f'<a href="{cta_url}" target="_blank" style="display:inline-block;font-family:{F};font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;padding:12px 24px;">See it all in Vera &rarr;</a>'
        '<!--<![endif]-->'
        '</td></tr></table></td></tr>'
    )

    lead = (f"{exposed:,} credentials exposed" if exposed
            else highlights[0]["headline"] if highlights else "free report")
    preheader = e(f"{name} — {lead}")[:140]

    return f"""<!DOCTYPE html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="x-apple-disable-message-reformatting">
<title>Vera &mdash; {e(name)}</title>
<!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
</head>
<body style="margin:0;padding:0;background-color:#f3f3f1;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;line-height:1px;color:#f3f3f1;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f3f3f1;"><tr><td align="center" style="padding:36px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="width:600px;max-width:600px;background-color:#ffffff;border:1px solid #e9e9e7;border-radius:16px;">
<tr><td style="padding:30px 36px 0 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td align="left" style="font-family:{F};font-size:13px;color:{PRI};font-weight:600;">Vera <span style="color:{MUT};font-weight:400;">&middot; free report</span></td>
<td align="right" style="font-family:{F};font-size:12px;color:{MUT};">{e(domain)}</td>
</tr></table></td></tr>
<tr><td style="padding:26px 36px 0 36px;">
<div style="font-family:{F};font-size:27px;font-weight:600;letter-spacing:-0.025em;color:{PRI};line-height:1.1;">{e(name)}</div>
{f'<div style="font-family:{F};font-size:13px;color:{MUT};padding-top:5px;">{industry}</div>' if industry else ''}
</td></tr>
<tr><td style="padding:0 36px 30px 36px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
{desc_block}
{facts_block}
{team_block}
{hiring_block}
{pages_block}
{perimeter_block}
{exposure_block}
{posture_block}
{signals_block}
{stack_block}
{locked_block}
{cta_block}
</table>
</td></tr>
<tr><td style="padding:0 36px 30px 36px;">
<div style="font-family:{F};font-size:11px;line-height:1.6;color:{MUT};border-top:1px solid {HAIR};padding-top:18px;">Company intelligence from public sources. No individuals named.</div>
<div style="font-family:{F};font-size:11px;color:{MUT};padding-top:8px;">Vera &middot; <a href="{settings.DASHBOARD_URL}" style="color:#0a0a0a;text-decoration:none;">{settings.DASHBOARD_URL}</a></div>
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def send_free_report_email(*, to_email: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """Email the free company report. Company-level only; no contact data."""
    _add_to_resend_audience(to_email)  # best-effort; enrols the lead for follow-ups
    return _send_via_resend(
        to_email=to_email,
        subject=_build_free_report_subject(report),
        html=_build_free_report_html(report),
    )


def send_welcome_email(
    *,
    to_email: str,
    company_name: str,
    tier: str,
    cycle: str,
    magic_link: str,
    contact_name: str = "",
    needs_password: bool = True,
) -> Dict[str, Any]:
    """Send the post-signup welcome email.

    Caller has already created the auth user and (when a password still
    needs setting) produced `magic_link` via Supabase's admin.generate_link.
    We don't generate it here so this module stays pure-render (one job).

    `needs_password` controls the primary CTA, not the tier: when True the
    email carries the magic-link "set your password" button (abandoned
    /welcome tab, or teammate invite); when False it shows a plain "sign
    in" link because the customer already set their password on /welcome.
    """
    subject = (
        "Welcome to Vera — set your password to get started"
        if needs_password
        else "Welcome to Vera"
    )
    html = build_welcome_html(
        company_name=company_name,
        tier=tier,
        cycle=cycle,
        magic_link=magic_link,
        cal_url=settings.ONBOARDING_CAL_URL,
        dashboard_url=settings.DASHBOARD_URL,
        contact_name=contact_name,
        needs_password=needs_password,
    )
    text = build_welcome_text(
        company_name=company_name,
        tier=tier,
        magic_link=magic_link,
        cal_url=settings.ONBOARDING_CAL_URL,
        contact_name=contact_name,
        needs_password=needs_password,
        dashboard_url=settings.DASHBOARD_URL,
    )
    return _send_via_resend(to_email=to_email, subject=subject, html=html, text=text)


def send_founder_email(
    *,
    to_email: str,
    contact_name: str = "",
    company_name: str = "",
    scheduled_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Send (or schedule) the personal founder follow-up.

    Called right after the welcome email on owner signup, with
    `scheduled_at` ~10 minutes out so it lands as a personal note shortly
    after the customer is in. Sends as "The Vera team" from the verified
    domain address.
    """
    html = build_founder_html(
        contact_name=contact_name,
        company_name=company_name,
        cal_url=settings.ONBOARDING_CAL_URL,
    )
    text = build_founder_text(
        contact_name=contact_name,
        company_name=company_name,
        cal_url=settings.ONBOARDING_CAL_URL,
    )
    return _send_via_resend(
        to_email=to_email,
        subject=FOUNDER_SUBJECT,
        html=html,
        text=text,
        scheduled_at=scheduled_at,
        from_name="The Vera team",
    )


def send_trial_ending_email(
    *,
    to_email: str,
    first_name: str = "",
    days_left: int = 3,
    price_label: str = "",
    manage_url: str = f"{settings.DASHBOARD_URL}/dashboard/settings",
) -> Dict[str, Any]:
    """The personal trial-ending note (2026-07-23), sent from Resend
    instead of Stripe's transactional default so it reads like a person
    wrote it.

    Warm and low-pressure, with the honest bit card-network rules require:
    what happens on day 8 and how to cancel. The real ask is feedback,
    either way.
    """
    greet = f"Hey {first_name}," if first_name else "Hey,"
    _words = {2: "in two days", 3: "in three days", 4: "in four days", 5: "in five days"}
    when = "tomorrow" if days_left <= 1 else _words.get(days_left, f"in {days_left} days")
    charge_line = (
        f"After that your plan starts at {price_label} a month and Vera keeps watching your accounts."
        if price_label
        else "After that your plan starts and Vera keeps watching your accounts."
    )
    subject = f"Your Vera trial ends {when}"

    text = (
        f"{greet}\n\n"
        "Thanks so much for giving Vera a go this week. It genuinely means a lot.\n\n"
        f"Quick heads up: your trial ends {when}. {charge_line} Nothing you need to do to continue.\n\n"
        "We'd love you to stay. Every early customer helps us keep building and "
        "improving Vera, and you'd be a real part of that. But if it isn't right "
        "for you, honestly no hard feelings, you can cancel any time from Settings.\n\n"
        "Either way, would you do us one favour? Just hit reply and tell us what "
        "you thought. What worked, what didn't, what was missing. That feedback is "
        "worth more to us than almost anything.\n\n"
        f"Manage or cancel your plan: {manage_url}\n\n"
        "Thanks again,\nThe Vera team"
    )

    p = 'style="margin:0 0 14px;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.6;color:#1a1a1a"'
    html = (
        '<div style="max-width:520px">'
        f"<p {p}>{_esc(greet)}</p>"
        f"<p {p}>Thanks so much for giving Vera a go this week. It genuinely means a lot.</p>"
        f"<p {p}>Quick heads up: your trial ends {when}. {charge_line} Nothing you need to do to continue.</p>"
        f"<p {p}>We'd love you to stay. Every early customer helps us keep building and improving Vera, "
        "and you'd be a real part of that. But if it isn't right for you, honestly no hard feelings, "
        "you can cancel any time from Settings.</p>"
        f"<p {p}>Either way, would you do us one favour? Just hit reply and tell us what you thought. "
        "What worked, what didn't, what was missing. That feedback is worth more to us than almost anything.</p>"
        f'<p {p}><a href="{_esc(manage_url)}" style="color:#7c3aed">Manage or cancel your plan</a></p>'
        f"<p {p}>Thanks again,<br>The Vera team</p>"
        "</div>"
    )
    return _send_via_resend(
        to_email=to_email, subject=subject, html=html, text=text, from_name="The Vera team",
    )
