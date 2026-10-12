from typing import Optional, Any, Dict, List


def _format_services_list(services: List[str]) -> str:
    """Format services_offered list into readable string."""
    if not services:
        return "N/A"
    return ", ".join(services)


ENRICHMENT_CYBER_GTM_PROMPT = """Analyse this cybersecurity company's website and extract indicators of their go-to-market maturity. I want to understand if this company would benefit from automated pipeline generation services.

## Website content from {domain}:
{page_content}

## What to look for

The content above may include multiple pages — homepage, /about, /careers, /case-studies, /customers, /security, /trust, /compliance, /soc2, /iso27001, etc. — each prefaced with "--- Page: <path> ---". Use whichever pages contain the relevant information.

Specifically:
- **Case studies / customers**: scan /case-studies, /customers, /clients, /success-stories, /testimonials. Pull out customer industries (financial services, healthcare, etc.) and named customers if visible. These reveal the company's proven ICP.
- **Compliance & certifications**: scan /security, /trust, /trust-centre, /compliance, /soc2, /iso27001, /gdpr, /hipaa. List every certification the company publicly claims (SOC 2 Type I/II, ISO 27001, ISO 27017, PCI-DSS Level 1, HIPAA, FedRAMP, Cyber Essentials, etc.) and every compliance framework referenced (GDPR, CCPA, NIS2, DORA). Set has_trust_centre=true if there's a dedicated trust/security page. A page labelled "(external trust centre)" is a hosted trust portal (SafeBase, Vanta, Drata, trust.<domain>) — it always counts as a trust centre and its certifications are authoritative.
- **Hiring signals**: scan /careers and /jobs for sales, BD, marketing, security roles.

Extract the following (use null/empty array if not found):

Respond ONLY with valid JSON:
{{
    "company_type": "<MSSP|pen_testing|threat_intel|GRC|consulting|product|mixed|other>",
    "services_offered": ["penetration testing", "managed SOC", "incident response"],
    "target_industries_mentioned": ["financial services", "healthcare"],
    "target_buyer_personas_mentioned": ["CISO", "IT Director", "CTO"],
    "has_sales_team_indicators": false,
    "has_dedicated_sales_page": false,
    "has_pricing_page": false,
    "has_demo_or_contact_form": true,
    "has_marketing_content": true,
    "has_blog": true,
    "blog_post_frequency": "<none|occasional|monthly|weekly>",
    "has_case_studies": false,
    "number_of_case_studies": 0,
    "case_study_industries": [],
    "case_study_clients": [],
    "has_testimonials": false,
    "client_logos_visible": false,
    "named_clients": [],
    "has_trust_centre": false,
    "certifications_mentioned": [],
    "compliance_frameworks": [],
    "has_careers_page": true,
    "open_roles_mentioned": [],
    "hiring_sales_or_bd": false,
    "hiring_marketing": false,
    "has_partner_page": false,
    "partner_names": [],
    "uses_crm_indicators": false,
    "uses_chat_widget": false,
    "uses_analytics_tools": true,
    "technology_mentions": [],
    "estimated_company_stage": "<pre_seed|seed|series_a|growth|established>",
    "estimated_gtm_maturity": "<none|basic|intermediate|advanced>",
    "estimated_team_size_from_site": "<1-5|5-15|15-50|50+|unknown>",
    "differentiator_claims": [],
    "pain_point_language_used": [],
    "notes": "<any observations about their sales/growth readiness and why they might need GTM help>"
}}"""


ENRICHMENT_SECURITY_PROMPT = """Analyse this company website content and extract cybersecurity-relevant information. I want to understand their security posture and whether they would be a good prospect for a cybersecurity vendor.

## Website content from {domain}:
{page_content}

Respond ONLY with valid JSON:
{{
    "certifications_mentioned": [],
    "compliance_frameworks": [],
    "has_security_page": false,
    "has_privacy_policy": false,
    "has_responsible_disclosure": false,
    "has_trust_centre": false,
    "security_team_indicators": false,
    "named_security_roles": [],
    "technology_mentions": [],
    "industry_vertical": "",
    "is_regulated_industry": false,
    "data_handling_indicators": [],
    "estimated_security_maturity": "<low|medium|high>",
    "security_vendor_mentions": [],
    "potential_gaps": [],
    "notes": "<observations about their security posture and buying potential>"
}}"""


LINKEDIN_CONTENT_PROMPT = """You are a sales intelligence analyst helping a salesperson at {client_name} prepare to reach out to {first_name} {last_name}, {title} at {company_name}.

## What {client_name} sells:
{what_they_sell}

## Buying signals {client_name} cares about:
{ideal_triggers}

## Recent LinkedIn posts from the prospect (numbered):
{posts_text}

Your job is to identify (a) buying signals in the prospect's content that map to what {client_name} sells, (b) specific personalisation hooks the SDR can reference in cold outreach, and (c) the FIVE posts most likely to be useful as outreach material for {client_name}.

A "personalisation hook" is a SPECIFIC thing the prospect said or shared that the SDR can naturally reference — not a generic observation. Bad: "talks about leadership". Good: "shared a Forbes article on AI security risks last Tuesday with the comment 'this is exactly why we built X'".

For the top_relevant_posts ranking, prioritise posts that:
1. Match one of {client_name}'s ideal buying signals above
2. Reveal a specific pain point {client_name} could solve
3. Show the prospect actively talking about a topic relevant to {client_name}'s product
4. Could serve as a natural opener for outreach

Refer to posts by their number (e.g. "Post 3"). Each top_relevant_posts entry must include the post's index (the number from the prompt) and a one-sentence why_it_matters that explicitly links the post to one of {client_name}'s buying signals or pain points.

Respond ONLY with valid JSON:
{{
    "posting_frequency": "<inactive|occasional|regular|very_active>",
    "average_engagement": "<low|medium|high>",
    "primary_topics": ["<topic1>", "<topic2>"],
    "mentions_growth": false,
    "mentions_sales_or_pipeline": false,
    "mentions_hiring": false,
    "mentions_funding": false,
    "mentions_challenges": false,
    "tone": "<technical|commercial|thought_leader|mixed>",
    "content_quality": "<basic|good|strong>",
    "personalisation_hooks": [
        "<specific reference: what they said + why it matters to {client_name}>"
    ],
    "buying_signals_for_client": [
        "<signal that maps to one of {client_name}'s ideal_triggers, with which post it came from>"
    ],
    "top_relevant_posts": [
        {{
            "post_index": <integer, the number from the numbered list above>,
            "why_it_matters": "<one sentence linking this post to {client_name}'s buying signals or pain points>"
        }}
    ],
    "recommended_approach": "<direct_pitch|value_add_content|mutual_connection|event_based>",
    "best_opening_line_angle": "<one specific sentence the SDR could open with, referencing a real post>",
    "notes": "<any other observations>"
}}"""


JOBS_ANALYSIS_PROMPT = """Analyse these job postings for a cybersecurity company. I want to understand their hiring activity and specifically whether they are hiring sales, business development, or marketing roles — which indicates they are actively trying to grow their pipeline.

## Company: {company_name} ({domain})

## Job postings found:

### From LinkedIn (via Apify):
{linkedin_jobs}

### From company website:
{website_jobs}

### From Apollo:
{apollo_jobs}

### From Indeed:
{indeed_jobs}

Respond ONLY with valid JSON, no other text:
{{
    "total_roles_found": <number>,
    "roles": [
        {{
            "title": "<job title>",
            "source": "<linkedin|website|apollo|indeed>",
            "location": "<location or remote>",
            "category": "<sales_bd|marketing|technical|security|grc|operations|leadership|other>",
            "is_growth_signal": <true if sales/BD/marketing/leadership hire>
        }}
    ],
    "is_hiring_sales_bd": <true|false>,
    "is_hiring_marketing": <true|false>,
    "is_hiring_technical": <true|false>,
    "is_hiring_leadership": <true|false>,
    "is_hiring_security": <true if ANY role with a cyber/security/infosec function is open. The definition is INTENTIONALLY WIDE — when in doubt, mark true. Examples that all count:
        — Leadership: CISO, CSO, Chief Information Security Officer, VP Security, VP Information Security, Head of Security, Head of Cyber, Head of Information Security, Head of Infosec, Director of Security, Director of Cybersecurity
        — Engineering: Security Engineer, Senior/Staff/Principal Security Engineer, Application Security (AppSec) Engineer, Product Security Engineer, Cloud Security Engineer, Infrastructure Security Engineer, Security Architect, Security Software Engineer
        — Offensive / Red team: Offensive Security Engineer, Penetration Tester, Red Team Engineer, Adversary Simulation Engineer
        — SOC / Operations / Detection: SOC Analyst, Security Operations Engineer, SIEM Engineer, Detection Engineer, Security Analyst
        — Incident response / Threat: Incident Response Engineer/Analyst, Threat Hunter, Threat Intelligence Analyst, Malware Analyst, Forensics Analyst
        — Identity / IAM: IAM Engineer, Identity Engineer/Architect, PAM Engineer, Access Governance Manager
        — InfoSec generalist: Information Security Analyst, Information Security Manager, Cyber Security Engineer
        — Threat intelligence: Cyber Threat Intelligence Analyst (CTI), TI Analyst
        — Forensics: Digital Forensics Analyst, Malware Reverse Engineer, DFIR Engineer
        — DevSecOps: DevSecOps Engineer, Secure SDLC Engineer
        Any title containing "security", "infosec", "cyber", "cybersecurity", "soc", "appsec", "siem", "iam", "pam", "ciso", "cso", "threat", "incident response", "dfir", "devsecops", "malware", or "forensics" is sufficient evidence. Mark true.

        EXCLUDE — these are NOT cyber and MUST NOT trigger this flag:
        — Physical security: Security Guard, Security Officer (when context is physical/building), Loss Prevention, Asset Protection, Surveillance Officer, Store Security, Event Security
        — Non-cyber forensics: Financial Forensics, Legal Forensics, Forensic Accountant
        — Non-cyber risk/audit: Credit Risk Analyst, Financial Risk Manager, Internal Audit (financial), Audit Senior (accounting)
        If a title is genuinely ambiguous ("Security Officer" with no other context, "Risk Manager" with no security framing), read the description preview to disambiguate — if it mentions cyber/IT/data/infrastructure/applications, include; if it mentions buildings/floor/physical/property/cash/credit, exclude.>,
    "is_hiring_grc": <true if ANY role with a GRC, compliance, risk, audit, or privacy function is open. Examples that all count:
        — Leadership: Head of GRC, VP GRC, VP Compliance, VP Risk, Chief Risk Officer, Chief Compliance Officer, DPO, Data Protection Officer, Privacy Officer
        — Practitioners: GRC Analyst/Manager, Compliance Manager/Officer, Security Compliance Manager, Risk Manager, Information Security Risk Manager, Security Audit Manager, Information Security Compliance Analyst
        — Privacy: Privacy Engineer, Privacy Counsel, Privacy Manager
        Any title containing "GRC", "compliance" (when in a security/risk context), "audit" (when in a security/risk/IT context), "risk" (when security/operational not financial), "DPO", "privacy" (when in a security/data-protection context) is sufficient evidence. Mark true.>,
    "hiring_velocity": "<none|low|moderate|high>",
    "hiring_velocity_notes": "<e.g. '5 open roles for a 20-person company suggests rapid growth'>",
    "growth_stage_indicator": "<stable|growing|rapidly_scaling|restructuring>",
    "sales_bd_roles_found": [
        "<list of specific sales/BD/marketing role titles>"
    ],
    "security_roles_found": [
        "<list of specific security role titles, e.g. 'Senior Offensive Security Engineer'>"
    ],
    "grc_roles_found": [
        "<list of specific GRC/compliance/risk role titles>"
    ],
    "signal_strength": <0.0 to 1.0 — higher if hiring sales/BD/security>,
    "personalisation_hooks": [
        "<e.g. 'Noticed you are hiring a BDR — we can generate pipeline while you build the team'>"
    ],
    "notes": "<any other observations about their hiring patterns>"
}}"""


NEWS_ANALYSIS_PROMPT = """Analyse these recent news articles about a cybersecurity company. Extract signals that indicate growth, activity, or potential need for pipeline generation services.

## Company: {company_name}
## Person of interest: {first_name} {last_name}, {title}

## Recent news articles:
{news_items}

Respond ONLY with valid JSON:
{{
    "news_found": true,
    "article_count": 0,
    "key_stories": [
        {{
            "headline": "",
            "source": "",
            "date": "",
            "category": "<funding|award|partnership|product_launch|hiring|breach_related|speaking|press_coverage|other>",
            "relevance_to_gtm": "<high|medium|low>",
            "summary": ""
        }}
    ],
    "overall_media_presence": "<none|minimal|moderate|strong>",
    "growth_indicators": [],
    "personalisation_hooks": [],
    "sentiment": "<positive|neutral|mixed|negative>",
    "notes": "<any other relevant observations>"
}}"""


# --- Contextual prompts (used when client services_offered is available) ---

CONTEXTUAL_WEBSITE_PROMPT = """You are analysing this company's website on behalf of {client_name}, a company that offers the following services:
{services_offered_list}

Company description: {client_positioning}

## Website content from {domain}:
{page_content}

Analyse the website from the perspective of whether this target company would benefit from {client_name}'s specific services. Focus on:
- Security posture gaps relevant to {services_offered_list}
- Missing certifications or compliance frameworks
- Indicators of security maturity (or lack thereof)
- Whether the company handles sensitive data that would require these services
- Any visible security team or leadership

IMPORTANT: If we already know the lead's name and title (provided below), cross-reference this with your website findings. If the lead IS a security leader but the website doesn't list them publicly, note this as "security leadership exists but is not publicly visible on the website" rather than "no security leadership detected."

Lead context (if available):
Name: {lead_name}
Title: {lead_title}

Respond ONLY with valid JSON:
{{
    "certifications_mentioned": [],
    "compliance_frameworks": [],
    "has_security_page": false,
    "has_privacy_policy": false,
    "has_responsible_disclosure": false,
    "has_trust_centre": false,
    "security_team_indicators": false,
    "named_security_roles": [],
    "security_leadership_notes": "<note if lead context reveals security leadership not visible on site>",
    "technology_mentions": [],
    "industry_vertical": "",
    "is_regulated_industry": false,
    "data_handling_indicators": [],
    "estimated_security_maturity": "<low|medium|high>",
    "security_vendor_mentions": [],
    "potential_gaps": [],
    "services_relevance": "<how {client_name}'s services specifically address gaps found>",
    "personalisation_hooks": [],
    "notes": "<observations about their security posture and buying potential for {client_name}'s services>"
}}"""


CONTEXTUAL_JOBS_PROMPT = """Analyse these job postings for {company_name} ({domain}).

The client analysing this company offers: {services_offered_list}

When analysing job postings, focus on signals relevant to the client's services:
- If the client sells security services: highlight security hiring (CISO, security engineers, compliance roles) as indicators the company is investing in security. Also flag if they're NOT hiring security roles despite being in a regulated industry — this suggests they may need external security services.
- Hiring velocity and growth stage are always relevant regardless of what the client sells.
- Generate personalisation hooks that reference the client's specific services, not generic GTM hooks.

For personalisation hooks, frame them around {services_offered_list}. Example:
- BAD (generic): "Noticed you're hiring — we can help generate leads"
- GOOD (contextual): "Noticed you're hiring a Head of Data Management — with new data leadership comes an opportunity to review application security practices. We specialise in pen testing for financial services companies."

## Job postings found:

### From LinkedIn (via Apify):
{linkedin_jobs}

### From company website:
{website_jobs}

### From Apollo:
{apollo_jobs}

### From Indeed:
{indeed_jobs}

Respond ONLY with valid JSON, no other text:
{{
    "total_roles_found": 0,
    "roles": [
        {{
            "title": "<job title>",
            "source": "<linkedin|website|apollo|indeed>",
            "location": "<location or remote>",
            "category": "<sales_bd|marketing|technical|security|operations|leadership|other>",
            "is_growth_signal": false
        }}
    ],
    "is_hiring_sales_bd": false,
    "is_hiring_marketing": false,
    "is_hiring_technical": false,
    "is_hiring_leadership": false,
    "is_hiring_security": false,
    "hiring_velocity": "<none|low|moderate|high>",
    "hiring_velocity_notes": "",
    "growth_stage_indicator": "<stable|growing|rapidly_scaling|restructuring>",
    "sales_bd_roles_found": [],
    "security_roles_found": [],
    "signal_strength": 0.0,
    "personalisation_hooks": [],
    "notes": "<observations about hiring patterns relevant to {services_offered_list}>"
}}"""


CONTEXTUAL_NEWS_PROMPT = """Analyse these recent news articles about {company_name}.

The client analysing this company offers: {services_offered_list}
Client description: {client_positioning}

Person of interest: {first_name} {last_name}, {title}

When analysing news, focus on events that create buying opportunities for the client's specific services:
- If the client sells security services: funding rounds (budget for security), new CTOs/CISOs (security infrastructure reviews), partnerships (integration security), regulatory actions (compliance urgency), data breaches (competitor incidents)
- Generate personalisation hooks that reference the client's services specifically

For personalisation hooks, always tie back to {services_offered_list}. Example:
- BAD: "Recent CTO appointment may create opportunities"
- GOOD: "New CTO Jane Smith will likely review the security testing programme — {client_name}'s services are directly relevant during technology leadership transitions"

## Recent news articles:
{news_items}

Respond ONLY with valid JSON:
{{
    "news_found": true,
    "article_count": 0,
    "key_stories": [
        {{
            "headline": "",
            "source": "",
            "date": "",
            "category": "<funding|award|partnership|product_launch|hiring|breach_related|speaking|press_coverage|other>",
            "relevance_to_client_services": "<high|medium|low>",
            "summary": ""
        }}
    ],
    "overall_media_presence": "<none|minimal|moderate|strong>",
    "growth_indicators": [],
    "personalisation_hooks": [],
    "sentiment": "<positive|neutral|mixed|negative>",
    "notes": "<observations relevant to {services_offered_list}>"
}}"""


CONTEXTUAL_TECH_STACK_PROMPT = """Analyse this company's technology stack.

The client analysing this company offers these services: {services_offered_list}

Target company: {domain}
Technologies detected: {technologies}

Analyse the target company's technology stack from the perspective of what's relevant to the client's services.

If the client sells security services, focus on:
- Security tools and infrastructure detected (WAF, SIEM, EDR, DLP, IAM, vulnerability scanners)
- Missing security tooling that the client's services could address
- Compliance and governance tools
- Security maturity indicators from the tech stack

If the client sells GTM/marketing services, focus on:
- CRM and sales tools
- Marketing automation platforms
- Outbound email infrastructure
- Sales intelligence tools

If neither matches clearly, include both but label sections separately.

Respond ONLY with valid JSON:
{{
    "technologies_detected": [],
    "security_tools_detected": [],
    "security_gaps": [],
    "gtm_tools_detected": [],
    "gtm_gaps": [],
    "security_maturity_from_stack": "<none|basic|intermediate|advanced>",
    "gtm_maturity_from_stack": "<none|basic|intermediate|advanced>",
    "relevant_tools_for_client": "<tools relevant to {services_offered_list}>",
    "missing_tools_for_client": "<gaps relevant to {services_offered_list}>",
    "opportunity_notes": "<observations about how the client's services address gaps>",
    "personalisation_hooks": [],
    "notes": ""
}}"""


# ---------------------------------------------------------------------------
# Per-title cyber role classification (job-board collector, 2026-07-21)
#
# The weekly job-board collector needs per-ROLE verdicts ("they posted a
# Vulnerability Management Lead"), not the company-level yes/no that
# JOBS_ANALYSIS_PROMPT returns. Same taxonomy, same exclude list — the
# hard constraint is that every job source is gated by this one classifier,
# never a keyword match. That is how the old is_hiring_technical mapping
# counted backend developers as security hires.
#
# NOTE: JOBS_ANALYSIS_PROMPT above still carries its own inline copy of this
# taxonomy. Consolidating it onto this constant is a follow-up; if you edit
# the rules, edit BOTH until then.
# ---------------------------------------------------------------------------

CYBER_ROLE_TAXONOMY = """THE TEST: is the role's PRIMARY, day-to-day function protecting systems, data
or networks from cyber threats? Not "does the title contain a security-adjacent
word". If the main job is banking regulation, financial crime, insurance
product, law or general engineering, the answer is NO even when a security word
appears in the title.

When a title is ambiguous, mark BOTH flags false. A missed role costs nothing.
A false one puts a money-laundering vacancy on a cyber vendor's screen labelled
"security hiring", which destroys trust in every other row.

is_security = true ONLY for a hands-on cyber function:
  - Leadership: CISO, CSO, VP/Head/Director of (Information) Security or Cyber
  - Engineering: Security Engineer, AppSec, Product Security, Cloud Security,
    Infrastructure Security, Security Architect, Cryptography/Encryption Engineer
  - Offensive: Offensive Security, Penetration Tester, Red Team, Adversary Simulation
  - SOC/Detection: SOC Analyst, Security Operations, SIEM, Detection Engineer,
    Threat Detection, Security Awareness
  - IR/Threat: Incident Response, Threat Hunter, Threat Intelligence (CTI),
    Malware Analyst, Digital Forensics, DFIR
  - Identity: IAM/Identity/PAM Engineer or Architect, Access Management
  - Vulnerability: Vulnerability Management, Patch Management, Attack Surface
  - InfoSec generalist: Information Security Analyst/Manager/Engineer
  - DevSecOps: DevSecOps, Secure SDLC, Product Security Engineering

is_grc = true ONLY when the governance/risk/compliance/privacy work is
explicitly INFORMATION-SECURITY, TECHNOLOGY-RISK or DATA-PROTECTION work:
  - Head of GRC, Information Security Compliance Manager, Security Risk Manager,
    Security Audit Manager, Technology Risk (IT systems), IT Risk Manager
  - Data Protection Officer, Data Privacy Manager, Privacy Engineer
Generic corporate or financial compliance is NOT is_grc, however senior.

EXCLUDE ENTIRELY — both flags false. These are the observed false positives:
  - Financial crime: AML, MLRO, Money Laundering Reporting Officer, Fraud,
    Financial Crime Analyst, KYC, Sanctions, Export Controls, Trade Compliance
  - Financial/actuarial risk: Credit Risk, Market Risk, Model Risk, Liquidity
    Risk, Financial Risk Manager, Internal Audit (financial), Audit (accounting)
  - Regulatory//prudential compliance at banks, insurers, payments and fintechs:
    Regulatory Compliance, Regulatory Risk, Compliance Officer, Conduct Risk,
    Operational Resilience, Insurance Product / Forms & Compliance
  - CRYPTO MEANS CRYPTOCURRENCY unless the role is clearly cryptography or
    encryption engineering. "Crypto Compliance", "Digital Assets Compliance"
    and "Crypto Analyst" at a payments or FX firm are financial roles, NOT security.
  - Legal: Legal Counsel of any kind, including privacy or regulatory counsel
  - Physical security: Security Guard/Officer (buildings), Loss Prevention,
    Asset Protection, Surveillance, Store or Event Security
  - General engineering with no security remit: Kernel Engineer, Backend,
    Platform, Data, SRE, QA — even at a security company
  - Sales, marketing, recruiting, finance, HR, support

The category must describe what the role actually does. Never file a lawyer
under appsec or an engineer under grc; if nothing fits, use "other"."""


CYBER_ROLE_CLASSIFY_PROMPT = """Classify each job title below for a cybersecurity vendor's sales intelligence.

{taxonomy}

## Company: {company_name}

## Titles (index. title):
{titles}

For EVERY index, return a verdict. Category must be one of:
vuln_mgt | appsec | cloud_sec | soc | ir_threat | iam | grc | leadership | devsecops | other

Respond ONLY with valid JSON, no other text:
{{
    "roles": [
        {{"i": <index>, "is_security": <true|false>, "is_grc": <true|false>, "category": "<category>"}}
    ]
}}"""
