"""Research-path enrichment + pending-suggestion monitoring guards.

Pins the 2026-07-16 fixes (from a signup audit):

1. Accepted suggestions (research accounts, monitoring_active=false) get the
   same one-off first monitor pass the URL-paste path gets, so fresh domains
   don't stay base-data only forever (no why-this-matters, no profile score,
   no security team profile).
2. Pending suggestions are never monitored: the topup insert sets
   monitoring_active=False explicitly (the column default is true) and the
   batch monitor excludes status='pending' as a guard for legacy rows.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# run_one_off_monitor_pass — the shared research first-pass helper
# ---------------------------------------------------------------------------
class TestRunOneOffMonitorPass:
    def _db(self, wl_rows, org_rows):
        db = MagicMock()
        wl_q = MagicMock()
        (
            wl_q.select.return_value.eq.return_value.eq.return_value
            .limit.return_value.execute.return_value
        ) = MagicMock(data=wl_rows)
        org_q = MagicMock()
        (
            org_q.select.return_value.eq.return_value
            .limit.return_value.execute.return_value
        ) = MagicMock(data=org_rows)
        db.table.side_effect = (
            lambda name: wl_q if name == "watchlist" else org_q
        )
        return db

    def test_first_run_when_no_snapshot(self):
        from app.services.watchlist.monitor import run_one_off_monitor_pass

        entry = {
            "id": "wl-1", "org_id": "org-1", "company_domain": "acme.com",
            "latest_snapshot": None, "last_monitored_at": None,
        }
        org = {"name": "Org", "icp_config": {}, "client_context": {}}
        db = self._db([entry], [org])
        with patch("app.database.get_supabase", return_value=db), \
                patch("app.services.watchlist.monitor.monitor_account") as mock_mon:
            run_one_off_monitor_pass("org-1", "acme.com")

        mock_mon.assert_called_once()
        assert mock_mon.call_args.kwargs["is_first_run"] is True
        assert mock_mon.call_args.args[1] is entry

    def test_not_first_run_when_snapshot_exists(self):
        from app.services.watchlist.monitor import run_one_off_monitor_pass

        entry = {
            "id": "wl-1", "org_id": "org-1", "company_domain": "acme.com",
            "latest_snapshot": {"k": "v"},
            "last_monitored_at": "2026-01-01T00:00:00+00:00",
        }
        org = {"name": "Org", "icp_config": {}, "client_context": {}}
        db = self._db([entry], [org])
        with patch("app.database.get_supabase", return_value=db), \
                patch("app.services.watchlist.monitor.monitor_account") as mock_mon:
            run_one_off_monitor_pass("org-1", "acme.com")

        mock_mon.assert_called_once()
        assert mock_mon.call_args.kwargs["is_first_run"] is False

    def test_debounced_within_the_hour(self):
        from datetime import datetime, timezone
        from app.services.watchlist.monitor import run_one_off_monitor_pass

        entry = {
            "id": "wl-1", "org_id": "org-1", "company_domain": "acme.com",
            "latest_snapshot": None,
            "last_monitored_at": datetime.now(timezone.utc).isoformat(),
        }
        db = self._db([entry], [{"name": "Org"}])
        with patch("app.database.get_supabase", return_value=db), \
                patch("app.services.watchlist.monitor.monitor_account") as mock_mon:
            run_one_off_monitor_pass("org-1", "acme.com")

        mock_mon.assert_not_called()

    def test_skips_when_no_watchlist_row(self):
        from app.services.watchlist.monitor import run_one_off_monitor_pass

        db = self._db([], [{"name": "Org"}])
        with patch("app.database.get_supabase", return_value=db), \
                patch("app.services.watchlist.monitor.monitor_account") as mock_mon:
            run_one_off_monitor_pass("org-1", "acme.com")

        mock_mon.assert_not_called()

    def test_never_raises_out_of_background_task(self):
        from app.services.watchlist.monitor import run_one_off_monitor_pass

        entry = {
            "id": "wl-1", "org_id": "org-1", "company_domain": "acme.com",
            "latest_snapshot": None, "last_monitored_at": None,
        }
        db = self._db([entry], [{"name": "Org", "icp_config": {}, "client_context": {}}])
        with patch("app.database.get_supabase", return_value=db), \
                patch(
                    "app.services.watchlist.monitor.monitor_account",
                    side_effect=RuntimeError("boom"),
                ):
            run_one_off_monitor_pass("org-1", "acme.com")  # must not raise


# ---------------------------------------------------------------------------
# _research_enrich — suggestion-accept research path
# ---------------------------------------------------------------------------
class TestResearchEnrichRunsFirstPass:
    def test_enrich_then_first_pass(self):
        from app.routers.account_suggestions import _research_enrich

        with patch("app.database.get_supabase", return_value=MagicMock()), \
                patch("app.services.company_enricher.enrich_company_only") as mock_enrich, \
                patch("app.services.watchlist.monitor.run_one_off_monitor_pass") as mock_pass:
            _research_enrich("org-1", "acme.com")

        mock_enrich.assert_called_once()
        assert mock_enrich.call_args.args[1:] == ("org-1", "acme.com")
        mock_pass.assert_called_once_with("org-1", "acme.com")

    def test_first_pass_runs_even_when_enrich_fails(self):
        from app.routers.account_suggestions import _research_enrich

        with patch("app.database.get_supabase", return_value=MagicMock()), \
                patch(
                    "app.services.company_enricher.enrich_company_only",
                    side_effect=RuntimeError("apollo down"),
                ), \
                patch("app.services.watchlist.monitor.run_one_off_monitor_pass") as mock_pass:
            _research_enrich("org-1", "acme.com")

        mock_pass.assert_called_once_with("org-1", "acme.com")


# ---------------------------------------------------------------------------
# Topup insert — pending suggestions must never be born monitored
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class _FakeQuery:
    """Any chained filter method returns self; execute returns the canned
    result; insert payloads are recorded."""

    def __init__(self, result, insert_log=None):
        self._result = result
        self._insert_log = insert_log

    def insert(self, payload):
        if self._insert_log is not None:
            self._insert_log.append(payload)
        return self

    def execute(self):
        return self._result

    def __getattr__(self, _name):
        return lambda *a, **k: self


class _FakeDB:
    def __init__(self, watchlist_results, org_result):
        self.insert_log = []
        self._watchlist_results = list(watchlist_results)
        self._org_result = org_result

    def table(self, name):
        if name == "organisations":
            return _FakeQuery(self._org_result)
        res = (
            self._watchlist_results.pop(0)
            if self._watchlist_results
            else _FakeResult(data=[])
        )
        return _FakeQuery(res, insert_log=self.insert_log)


class TestTopupInsertsAreNotMonitored:
    def test_pending_insert_sets_monitoring_active_false(self):
        from app.routers import account_suggestions as mod

        db = _FakeDB(
            watchlist_results=[
                _FakeResult(data=[], count=0),          # pending count
                _FakeResult(data=[]),                   # skip-domain scan
                _FakeResult(data=[{"id": "sugg-1"}]),   # insert result
            ],
            org_result=_FakeResult(data={"icp_config": {}}),
        )
        candidate = {"name": "Acme", "domain": "acme.com", "matched_reasons": ["r"]}
        with patch.object(mod, "get_supabase", return_value=db), \
                patch.object(mod, "_get_icp_criteria", return_value={
                    "industries": ["cyber"], "locations": [], "employee_ranges": [],
                }), \
                patch.object(mod, "build_seller_profile", return_value="profile"), \
                patch.object(mod, "search_companies", return_value=[candidate]), \
                patch.object(mod, "classify_candidates", return_value=[candidate]), \
                patch("app.services.webhooks.enqueue_event"):
            out = mod._topup_pool_inner("org-1", target=1)

        assert out["added"] == 1
        assert len(db.insert_log) == 1
        row = db.insert_log[0]
        assert row["status"] == "pending"
        assert row["monitoring_active"] is False


# ---------------------------------------------------------------------------
# Batch monitor — pending rows are excluded even if monitoring_active=true
# ---------------------------------------------------------------------------
class TestBatchMonitorExcludesPending:
    def test_per_org_query_filters_out_pending(self):
        from app.services.watchlist.batch_monitor import run_daily_monitoring

        db = MagicMock()
        org_q = MagicMock()
        org_q.select.return_value.eq.return_value.execute.return_value = MagicMock(
            data=[{"id": "org-1", "name": "Org", "icp_config": {}}]
        )
        wl_q = MagicMock()
        neq = wl_q.select.return_value.eq.return_value.eq.return_value.neq
        (
            neq.return_value.or_.return_value.order.return_value
            .limit.return_value.execute.return_value
        ) = MagicMock(data=[])
        db.table.side_effect = (
            lambda name: org_q if name == "organisations" else wl_q
        )
        with patch(
            "app.services.watchlist.person_monitor.monitor_watched_people",
            return_value={"watched": 0},
        ), patch("app.services.watchlist.batch_monitor.log_activity"):
            result = run_daily_monitoring(db, "org-1")

        neq.assert_called_once_with("status", "pending")
        assert result["accounts_checked"] == 0

    def test_org_scan_filters_out_pending(self):
        from app.services.watchlist.batch_monitor import run_monitoring_all_orgs

        db = MagicMock()
        wl_q = MagicMock()
        wl_q.select.return_value.eq.return_value.neq.return_value.execute.return_value = MagicMock(data=[])
        leads_q = MagicMock()
        leads_q.select.return_value.eq.return_value.execute.return_value = MagicMock(data=[])
        db.table.side_effect = (
            lambda name: wl_q if name == "watchlist" else leads_q
        )
        run_monitoring_all_orgs(db)

        wl_q.select.return_value.eq.return_value.neq.assert_called_once_with(
            "status", "pending"
        )


# ---------------------------------------------------------------------------
# Weekly why-this-matters sweep — /accounts/why-this-matters/refresh-solo
# ---------------------------------------------------------------------------
class TestWhyRefreshSolo:
    def _reset_debounce(self, mod):
        mod._WHY_SOLO_LAST_RUN["ts"] = 0.0

    def test_trigger_starts_then_debounces(self):
        from app.routers import account_suggestions as mod

        self._reset_debounce(mod)
        tasks = MagicMock()
        first = mod.refresh_why_this_matters_solo(tasks)
        second = mod.refresh_why_this_matters_solo(tasks)
        self._reset_debounce(mod)

        assert first == {"status": "started", "scope": "solo"}
        assert second == {"status": "debounced"}
        tasks.add_task.assert_called_once_with(mod._run_why_refresh_solo_bg)

    def test_sweep_covers_live_capped_orgs_and_skips_revoked(self):
        from app.routers import account_suggestions as mod

        db = MagicMock()
        db.table.return_value.select.return_value.in_.return_value.execute.return_value = MagicMock(
            data=[
                {"id": "org-live", "subscription_status": "active"},
                {"id": "org-lapsed", "subscription_status": "canceled"},
            ]
        )
        with patch.object(mod, "get_supabase", return_value=db), \
                patch.object(mod, "_refresh_why_all_for_org", return_value=3) as mock_refresh, \
                patch("app.services.plan_limits.REVOKED_STATUSES", {"canceled"}), \
                patch("app.services.plan_limits.CAPPED_TIERS", ("solo", "starter")):
            mod._run_why_refresh_solo_bg()

        mock_refresh.assert_called_once_with(db, "org-live")

    def test_sweep_never_raises(self):
        from app.routers import account_suggestions as mod

        with patch.object(mod, "get_supabase", side_effect=RuntimeError("db down")):
            mod._run_why_refresh_solo_bg()  # must not raise
