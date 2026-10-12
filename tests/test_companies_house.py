"""Tests for Companies House API parsing."""

from app.services.enrichers.companies_house import (
    _is_within_months,
    _map_accounts_to_revenue,
    _parse_companies_house_data,
    _years_since,
)


class TestCompaniesHouseParsing:
    """Test Companies House data parsing."""

    def test_map_micro_entity(self):
        assert _map_accounts_to_revenue("micro-entity") == "Under £632k"

    def test_map_small(self):
        assert _map_accounts_to_revenue("small") == "£632k - £10.2m"

    def test_map_medium(self):
        assert _map_accounts_to_revenue("medium") == "£10.2m - £36m"

    def test_map_unknown(self):
        assert _map_accounts_to_revenue("something_else") == "Unknown"

    def test_years_since_valid_date(self):
        years = _years_since("2020-01-01")
        assert years is not None
        assert years >= 5

    def test_years_since_none(self):
        assert _years_since(None) is None

    def test_is_within_months_recent(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        assert _is_within_months(recent, 6) is True

    def test_is_within_months_old(self):
        assert _is_within_months("2020-01-01", 6) is False

    def test_is_within_months_none(self):
        assert _is_within_months(None, 6) is False

    def test_parse_full_company_data(self):
        company_data = {
            "company_number": "12345678",
            "company_name": "NORTHWIND LTD",
            "date_of_creation": "2020-06-15",
            "company_status": "active",
            "sic_codes": ["62012"],
            "accounts": {
                "last_accounts": {"type": "micro-entity"}
            },
            "registered_office_address": {"locality": "Manchester"},
        }
        filings = [{"date": "2025-12-01"}]
        officers = [
            {
                "name": "EXAMPLE, Alex",
                "officer_role": "director",
                "appointed_on": "2020-06-15",
            }
        ]

        result = _parse_companies_house_data(company_data, filings, officers)
        assert result["company_number"] == "12345678"
        assert result["company_name"] == "NORTHWIND LTD"
        assert result["company_status"] == "active"
        assert result["estimated_revenue_bracket"] == "Under £632k"
        assert result["latest_filing_date"] == "2025-12-01"
        assert len(result["current_officers"]) == 1

    def test_officer_change_detection(self):
        """Recent officer appointment should be flagged."""
        from datetime import datetime, timezone, timedelta

        recent_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")

        company_data = {
            "company_number": "99999",
            "company_name": "TEST LTD",
            "company_status": "active",
            "accounts": {},
        }
        officers = [
            {
                "name": "NEW DIRECTOR",
                "officer_role": "director",
                "appointed_on": recent_date,
            }
        ]

        result = _parse_companies_house_data(company_data, [], officers)
        assert result["recent_officer_changes"] == 1
        assert result["current_officers"][0]["is_recent_appointment"] is True
