THREAT_ANALYSIS_PROMPT = """Analyse this article from a cybersecurity intelligence feed. The feed mixes threats (CVEs, breaches, ransomware) with commercial signals (funding rounds, M&A, hiring) — your job is to classify the article AND extract context for downstream sales matching.

## Article Details:
Source: {source}
Title: {title}
Description: {description}
Severity: {severity}
URL: {url}

Respond ONLY with valid JSON, no other text:
{{
    "category": "<news_funding|news_acquisition|news_hiring|news_product_launch|news_breach|news_ransomware|news_vulnerability|news_enforcement|news_other — pick the single best fit>",
    "ai_summary": "<2-3 sentence plain English summary of what this article means for businesses>",
    "affected_industries": ["<list of industries likely affected or relevant — e.g. financial services, healthcare, manufacturing, retail, government, education, legal, energy, transport, cybersecurity, SaaS>"],
    "affected_technologies": ["<list of technologies/products affected — e.g. Microsoft Exchange, Apache, WordPress, Cisco, Fortinet, VMware>"],
    "affected_vendors": ["<list of specific vendor or company names mentioned>"],
    "threat_type": "<vulnerability|breach|advisory|enforcement|exploit|commercial — use 'commercial' for funding/M&A/hiring/product news>",
    "business_impact": "<what this means for non-technical business leaders>",
    "urgency_for_sales": "<critical|high|medium|low — how urgently should a vendor act on this>",
    "is_actively_exploited": <true|false>,
    "compliance_relevance": ["<relevant frameworks — NIS2, DORA, Cyber Essentials, ISO 27001, PCI-DSS, GDPR>"]
}}"""

THREAT_SALES_PROMPT = """A cybersecurity threat has been identified that is relevant to a cybersecurity company's target market. Generate a sales-focused alert that helps their team take advantage of this opportunity — sensitively and professionally, not predatory.
{no_fear_directive}
## The Threat:
{threat_summary}

## The Cybersecurity Company:
Name: {org_name}
Services they sell: {org_services}
Their target market: {icp_description}

## Why it's relevant:
The threat affects these industries that their prospects operate in: {matched_industries}
The threat affects these technologies their prospects use: {matched_technologies}
Watchlist companies potentially affected: {matched_watchlist}

Respond ONLY with valid JSON, no other text:
{{
    "relevance_reason": "<1-2 sentences explaining why this matters to this specific cybersecurity company>",
    "sales_angle": "<specific approach — not 'you should reach out' but exactly what to say and why>",
    "suggested_outreach": "<a 2-3 sentence email opener or LinkedIn message that references the threat without being predatory>",
    "content_hook": "<a LinkedIn post idea that positions the company as an expert on this threat>",
    "urgency": "<critical|high|medium|low>",
    "affected_services": ["<which of the company's services are most relevant to this threat>"]
}}"""
