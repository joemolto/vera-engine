"""Guards for the 'people waiting' retention nudge in the solo digest.

The join that matters: locked (not-yet-unlocked) key_contacts at watched
accounts, ranked by ICP relevance, deduped by cooldown. A bug here either
leaks an ALREADY-unlocked contact (embarrassing) or silently shows nobody.
"""
from app.config import settings
from app.services import solo_digest as D


def _watch(domain, name):
    return {"company_domain": domain, "company_name": name}


def _kc(name, score, *, unlocked=False, lead_id=None, title="Head of Security", reason="runs the SOC"):
    row = {"name": name, "title": title, "icp_score": score, "relevance_reason": reason}
    if unlocked:
        row["unlocked"] = True
    if lead_id:
        row["lead_id"] = lead_id
    return row


def test_locked_only_ranked_and_capped(mock_db):
    mock_db.set_table_data("watchlist", [_watch("monzo.com", "Monzo"), _watch("starling.com", "Starling")])
    mock_db.set_table_data("companies", [
        {"domain": "monzo.com", "name": "Monzo", "key_contacts": [
            _kc("A", 0.95),
            _kc("B", 0.90),
            _kc("C", 0.85),                       # 3rd at Monzo — dropped by max-2-per-company
            _kc("UNLOCKED", 0.99, unlocked=True),  # already unlocked — never nudged
            _kc("HASLEAD", 0.99, lead_id="l1"),    # has a lead — already actioned
        ]},
        {"domain": "starling.com", "name": "Starling", "key_contacts": [_kc("D", 0.80)]},
    ])
    out = D._waiting_contacts(mock_db, "org1", exclude_keys=set())
    names = [c["name"] for c in out]

    assert "UNLOCKED" not in names and "HASLEAD" not in names  # locked only
    assert names == ["A", "B", "D"]  # score desc, max 2 per company (C dropped), <= _MAX_WAITING
    assert out[0]["company"] == "Monzo" and out[0]["reason"] == "runs the SOC"


def test_cooldown_excludes_recently_surfaced(mock_db):
    mock_db.set_table_data("watchlist", [_watch("monzo.com", "Monzo")])
    mock_db.set_table_data("companies", [
        {"domain": "monzo.com", "name": "Monzo", "key_contacts": [_kc("A", 0.95), _kc("B", 0.90)]},
    ])
    # key format mirrors _waiting_contacts: "{domain}:{name_lower}" when no apollo_id
    out = D._waiting_contacts(mock_db, "org1", exclude_keys={"monzo.com:a"})
    assert [c["name"] for c in out] == ["B"]  # A is on cooldown


def test_fragment_links_to_unlock_and_escapes_html():
    waiting = [{
        "key": "k", "name": "Sarah <script>", "domain": "monzo.com",
        "company": "Monzo", "title": "CISO", "reason": "budget owner",
    }]
    text, html = D._waiting_fragment(waiting)
    url = f"{settings.DASHBOARD_URL}/dashboard/accounts/monzo.com"
    assert url in text
    assert "Unlock" in html and url in html
    assert "<script>" not in html and "&lt;script&gt;" in html  # name is escaped


def test_render_waiting_subject_singular_vs_plural():
    one = [{"key": "k", "name": "Sarah", "domain": "d", "company": "Monzo", "title": "CISO", "reason": ""}]
    s1, _, _ = D._render_waiting("Alex", one, None)
    assert s1 == "Sarah at Monzo is worth a look"

    two = one + [{"key": "k2", "name": "Dan", "domain": "d2", "company": "Contoso", "title": "CTO", "reason": ""}]
    s2, _, _ = D._render_waiting("Alex", two, None)
    assert s2 == "2 people worth reaching at your accounts"
