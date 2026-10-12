"""Regression tests for the 2026-07-09 user-simulation findings.

Three fixes, three groups:
1. Account-signal deletes are org-scoped — an unscoped delete on the global
   companies row wiped every OTHER org's signals for the same domain
   (service role bypasses RLS).
2. monitor_account defines org_id — the webhook enqueue calls referenced a
   bare org_id that was never bound, so watchlist.alert_created and
   account.went_hot raised NameError (swallowed) on every pass.
3. SOLO_FEATURES grants solo_digest — the digest was gated to the demo org
   while every real Solo org skipped with reason=flag_off.
"""

from unittest.mock import MagicMock

ORG_ID = "org-aaa"
COMPANY_ID = "cmp-123"


# ---------------------------------------------------------------------------
# Recording mock — the shared MockSupabase ignores filters; these tests are
# specifically about which filters a query carries.
# ---------------------------------------------------------------------------

class RecordingQuery:
    def __init__(self, sink, table, data):
        self.__dict__["_sink"] = sink
        self.__dict__["_table"] = table
        self.__dict__["_data"] = data
        self.__dict__["_calls"] = []

    def execute(self):
        self._sink.append({"table": self._table, "calls": list(self._calls)})
        result = MagicMock()
        result.data = self._data
        result.count = len(self._data) if isinstance(self._data, list) else 1
        return result

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self._calls.append((name, args))
            return self

        return method


class RecordingSupabase:
    def __init__(self):
        self.executed = []
        self._table_data = {}

    def set_table_data(self, table, data):
        self._table_data[table] = data

    def table(self, name):
        return RecordingQuery(self.executed, name, self._table_data.get(name, []))

    def deletes(self, table):
        """Executed queries on `table` that included a .delete() step."""
        return [
            q for q in self.executed
            if q["table"] == table and any(c[0] == "delete" for c in q["calls"])
        ]


def _filters(query):
    return [c for c in query["calls"] if c[0] in ("eq", "is_", "in_")]


# ---------------------------------------------------------------------------
# 1. Org-scoped signal deletes
# ---------------------------------------------------------------------------

def test_write_account_level_signals_delete_is_org_scoped():
    from app.services.company_enricher import _write_account_level_signals

    db = RecordingSupabase()
    _write_account_level_signals(
        db,
        org_id=ORG_ID,
        company_id=COMPANY_ID,
        domain="example.com",
        pl_news=None,
        pl_financing=None,
        pl_jobs=None,
        threat_alerts=None,
        # Non-empty summary so rows project and the delete branch runs.
        credential_exposure={"recent_employees_90d": 2, "employees_compromised": 5},
    )

    deletes = db.deletes("signals")
    assert deletes, "expected the prior-signals cleanup delete to run"
    for q in deletes:
        filters = _filters(q)
        assert ("eq", ("org_id", ORG_ID)) in filters, (
            f"signals delete must be org-scoped, got filters: {filters}"
        )
        assert ("eq", ("company_id", COMPANY_ID)) in filters
        assert ("is_", ("lead_id", "null")) in filters


def test_refresh_credential_signals_delete_is_org_scoped():
    from app.services.enrichers.credential_exposure import refresh_credential_signals

    db = RecordingSupabase()
    refresh_credential_signals(
        db, org_id=ORG_ID, company_id=COMPANY_ID, summary=None
    )

    deletes = db.deletes("signals")
    assert deletes, "expected the credential-signals cleanup delete to run"
    filters = _filters(deletes[0])
    assert ("eq", ("org_id", ORG_ID)) in filters, (
        f"credential delete must be org-scoped, got filters: {filters}"
    )
    assert ("eq", ("company_id", COMPANY_ID)) in filters


# ---------------------------------------------------------------------------
# 2. monitor_account binds org_id (webhook NameError)
# ---------------------------------------------------------------------------

def test_monitor_account_binds_org_id_locally():
    """The bug: enqueue_event(db, org_id, ...) compiled org_id as a global
    load (never defined anywhere), raising NameError inside a swallowing
    try/except on every pass. Pin it at the bytecode level: org_id must be
    a local, and must never be loaded as a global."""
    import dis

    from app.services.watchlist.monitor import monitor_account

    assert "org_id" in monitor_account.__code__.co_varnames

    global_loads = {
        i.argval
        for i in dis.get_instructions(monitor_account)
        if i.opname == "LOAD_GLOBAL"
    }
    assert "org_id" not in global_loads


# ---------------------------------------------------------------------------
# 3. Solo digest is on for Solo orgs
# ---------------------------------------------------------------------------

def test_solo_features_include_solo_digest():
    from app.services.plan_limits import SOLO_FEATURES

    assert SOLO_FEATURES.get("solo_digest") is True


def test_digest_not_flag_skipped_for_solo_preset_org():
    """An org provisioned with the SOLO_FEATURES preset must get past the
    flag gate. No moves in the mock DB, so the digest lands on 'quiet',
    never 'skipped: flag_off'."""
    from app.services.plan_limits import SOLO_FEATURES
    from app.services.solo_digest import build_and_send

    db = RecordingSupabase()
    db.set_table_data(
        "organisations", {"icp_config": {"features": dict(SOLO_FEATURES)}}
    )

    result = build_and_send(db, ORG_ID)
    assert result.get("reason") != "flag_off", f"digest still gated off: {result}"
