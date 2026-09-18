"""Tests for cron/cw_contact_index.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cron.cw_contact_index import (
    contacts_matching,
    index_freshness,
    refresh_index,
    tickets_for_contact,
    trend_by_company,
    trend_by_requester,
)


class _FakeCWClient:
    def __init__(self, tickets):
        self._tickets = tickets

    def tickets_since(self, since_iso):
        return self._tickets


def _ticket(ticket_id, contact_name, contact_email, company="Henssler Financial", days_ago=1):
    entered = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": ticket_id,
        "_info": {"dateEntered": entered},
        "board": {"name": "Triage"},
        "status": {"name": "Open"},
        "priority": {"name": "Priority 3"},
        "summary": f"ticket {ticket_id}",
        "company": {"id": 250, "name": company},
        "contact": {"id": 1000 + ticket_id},
        "contactName": contact_name,
        "contactEmailAddress": contact_email,
    }


def test_refresh_index_populates_contact_fields(tmp_path):
    db_path = tmp_path / "index.db"
    tickets = [
        _ticket(1, "Erica Martin", "e@x.com"),
        _ticket(2, "Erica Martin", "e@x.com"),
        _ticket(3, "Jane Doe", "j@x.com", company="Other Co"),
    ]
    summary = refresh_index(days=30, client=_FakeCWClient(tickets), db_path=db_path)

    assert summary["tickets_pulled"] == 3
    assert summary["rows_upserted"] == 3
    assert summary["rows_with_contact"] == 3

    freshness = index_freshness(db_path=db_path)
    assert freshness["row_count"] == 3
    assert freshness["stale"] is False


def test_refresh_index_upserts_rather_than_duplicates(tmp_path):
    db_path = tmp_path / "index.db"
    refresh_index(days=30, client=_FakeCWClient([_ticket(1, "Erica Martin", "e@x.com")]), db_path=db_path)
    # Same ticket id, contact changed (e.g. reassigned) -- must update in place, not add a second row.
    refresh_index(days=30, client=_FakeCWClient([_ticket(1, "New Contact", "new@x.com")]), db_path=db_path)

    freshness = index_freshness(db_path=db_path)
    assert freshness["row_count"] == 1
    rows = tickets_for_contact(contact_email="new@x.com", db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["contact_name"] == "New Contact"


def test_index_freshness_empty_db_is_stale(tmp_path):
    db_path = tmp_path / "empty.db"
    freshness = index_freshness(db_path=db_path)
    assert freshness == {
        "row_count": 0,
        "last_refreshed_at": None,
        "window_days": None,
        "age_hours": None,
        "stale": True,
    }


def test_contacts_matching_partial_name_and_exact_email(tmp_path):
    db_path = tmp_path / "index.db"
    tickets = [_ticket(1, "Erica Martin", "e@x.com"), _ticket(2, "Erica Someone", "es@x.com")]
    refresh_index(days=30, client=_FakeCWClient(tickets), db_path=db_path)

    by_name = contacts_matching("Erica", db_path=db_path)
    assert {c["contact_email"] for c in by_name} == {"e@x.com", "es@x.com"}

    by_email = contacts_matching("e@x.com", db_path=db_path)
    assert by_email == [{"contact_name": "Erica Martin", "contact_email": "e@x.com"}]


def test_trend_by_requester_orders_by_count_and_respects_min_tickets(tmp_path):
    db_path = tmp_path / "index.db"
    tickets = [
        _ticket(1, "Frequent Filer", "f@x.com"),
        _ticket(2, "Frequent Filer", "f@x.com"),
        _ticket(3, "Frequent Filer", "f@x.com"),
        _ticket(4, "One Timer", "o@x.com"),
    ]
    refresh_index(days=30, client=_FakeCWClient(tickets), db_path=db_path)

    trend = trend_by_requester(days=30, min_tickets=2, db_path=db_path)
    assert trend == [{"contact_name": "Frequent Filer", "contact_email": "f@x.com", "company": "Henssler Financial", "ticket_count": 3}]


def test_trend_by_company_aggregates_across_contacts(tmp_path):
    db_path = tmp_path / "index.db"
    tickets = [
        _ticket(1, "Erica Martin", "e@x.com", company="Henssler Financial"),
        _ticket(2, "Jane Doe", "j@x.com", company="Henssler Financial"),
        _ticket(3, "Other Person", "o@x.com", company="Other Co"),
    ]
    refresh_index(days=30, client=_FakeCWClient(tickets), db_path=db_path)

    trend = trend_by_company(days=30, min_tickets=1, db_path=db_path)
    assert trend[0] == {"company": "Henssler Financial", "ticket_count": 2}


def test_tickets_for_contact_returns_nothing_without_identifier(tmp_path):
    db_path = tmp_path / "index.db"
    refresh_index(days=30, client=_FakeCWClient([_ticket(1, "Erica Martin", "e@x.com")]), db_path=db_path)
    assert tickets_for_contact(db_path=db_path) == []
