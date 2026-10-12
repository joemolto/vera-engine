"""What is this company exposing to the internet, and is any of it vulnerable?

On-demand research tool. The perimeter collector is deliberately NOT wired
into the enrichment pipeline (hit rate is roughly 1 strong finding per 75
accounts, too thin to run across everything, too useful to discard). Use
this by hand on accounts you actually care about.

Usage:
    PYTHONPATH=. python scripts/perimeter_check.py example.com
    PYTHONPATH=. python scripts/perimeter_check.py example.com example.org

What it does, and does not do
-----------------------------
Passive only. Reads Certificate Transparency (a public register every CA must
write to), resolves the hostnames it finds, and asks each one over HTTPS what
software it runs — the same request a browser makes. No port scanning, no
authentication attempts, no vulnerability probing. A 401 is recorded as "this
product is here" and never retried.

CVE claims require an observed version AND a verified vendor build mapping.
Products without one are reported as exposure only, never as vulnerable.

This product uses the NVD API but is not endorsed or certified by the NVD.
"""

import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

from app.services.cve_match import (  # noqa: E402
    NVD_ATTRIBUTION,
    build_cpe,
    lookup_cves,
)
from app.services.perimeter import scan_perimeter  # noqa: E402

KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)


def load_kev() -> set:
    try:
        r = httpx.get(KEV_URL, timeout=30)
        if r.status_code == 200:
            return {v["cveID"] for v in r.json().get("vulnerabilities", [])}
    except Exception as e:  # noqa: BLE001
        print(f"  (KEV list unavailable: {type(e).__name__})")
    return set()


def check(domain: str, kev: set) -> None:
    print(f"\n{'=' * 68}\n{domain}\n{'=' * 68}")

    findings = scan_perimeter(domain)
    if not findings:
        print("  Nothing identifiable exposed.")
        print("  Note: absence of a finding is not absence of exposure — most")
        print("  companies sit behind a CDN, and appliances hide their version.")
        return

    exposure, vulnerable = [], []
    for f in findings:
        cpe = build_cpe(f.product, f.version) if f.is_versioned else None
        if cpe:
            vulnerable.append((f, cpe))
        else:
            exposure.append(f)

    if vulnerable:
        print("\n  VERSIONED — CVE claim possible")
        for f, cpe in vulnerable:
            cves = lookup_cves(cpe, kev_ids=kev)
            kev_hits = [c for c in cves if c.is_kev]
            print(f"\n    {f.product} {f.version}")
            print(f"      host:     {f.host} ({f.ip})")
            print(f"      evidence: {f.evidence}")
            print(f"      matched:  {cpe}")
            if not cves:
                print("      CVEs:     none recorded against this build")
                continue
            print(f"      CVEs:     {len(cves)} total, {len(kev_hits)} actively exploited")
            for c in sorted(cves, key=lambda x: (not x.is_kev, x.published),
                            reverse=False)[:6]:
                flag = "KEV" if c.is_kev else "   "
                print(f"        {flag} {c.cve_id}  {c.published}  {c.severity or '-'}")

    if exposure:
        print("\n  EXPOSED — no version available, so no CVE claim")
        for f in exposure:
            v = f" {f.version}" if f.version else ""
            print(f"    {f.product}{v}")
            print(f"      host:     {f.host} ({f.ip})")
            print(f"      evidence: {f.evidence}")


def main() -> None:
    domains = [d.strip().lower() for d in sys.argv[1:] if d.strip()]
    if not domains:
        print(__doc__)
        raise SystemExit(1)

    print("Fetching CISA KEV catalogue ...")
    kev = load_kev()
    print(f"  {len(kev)} actively-exploited CVEs loaded")

    for d in domains:
        check(d, kev)

    print(f"\n{'-' * 68}\n{NVD_ATTRIBUTION}")


if __name__ == "__main__":
    main()
