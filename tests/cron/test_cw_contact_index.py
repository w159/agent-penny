"""Tests for cron/cw_contact_index.py."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from cron.cw_contact_index import (
    connect,
    contacts_matching,
    ensure_schema,
    index_freshness,
    merged_parent_ids,
    refresh_index,
    resolve_canonical_ticket_id,
    rollup_canonical_ticket_counts,
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
    assert trend == [
        {
            "contact_name": "Frequent Filer", "contact_email": "f@x.com", "company": "Henssler Financial",
            "ticket_count": 3, "related_ticket_ids": [],
        }
    ]


def _insert_row(conn, ticket_id, contact_name, contact_email, *, parent_ticket_id=None, has_merged_child_flag=0, company="Henssler Financial", days_ago=1):
    entered = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO tickets (id, date_entered, contact_name, contact_email, company_name, "
        "parent_ticket_id, has_merged_child_flag, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, entered, contact_name, contact_email, company, parent_ticket_id, has_merged_child_flag, entered),
    )


class TestResolveCanonicalTicketId:
    def test_child_resolves_to_parent(self):
        assert resolve_canonical_ticket_id({"id": 93980, "parent_ticket_id": 92827}) == 92827

    def test_parentless_ticket_resolves_to_own_id(self):
        assert resolve_canonical_ticket_id({"id": 92827, "parent_ticket_id": None}) == 92827


class TestRollupCanonicalTicketCounts:
    def test_linked_children_collapse_into_parent_count_and_list_as_related(self):
        rows = [
            {"id": 92827, "parent_ticket_id": None, "contact_name": "Scarlet Mendoza"},
            {"id": 93980, "parent_ticket_id": 92827, "contact_name": "Scarlet Mendoza"},
            {"id": 94325, "parent_ticket_id": 92827, "contact_name": "Scarlet Mendoza"},
        ]
        result = rollup_canonical_ticket_counts(rows, group_fields=("contact_name",))
        assert result == [
            {"contact_name": "Scarlet Mendoza", "ticket_count": 1, "related_ticket_ids": [93980, 94325]}
        ]


class TestTrendByRequesterRollup:
    def test_requesters_own_linked_children_count_once_not_twice(self, tmp_path):
        db_path = tmp_path / "index.db"
        conn = connect(db_path)
        # Nicole McFarland's own two tickets in the real Outlook cluster are
        # both children of #92827 (filed by a different contact) -- her
        # count must drop from 2 raw rows to 1 canonical id.
        _insert_row(conn, 93980, "Nicole McFarland", "n@x.com", parent_ticket_id=92827)
        _insert_row(conn, 94325, "Nicole McFarland", "n@x.com", parent_ticket_id=92827)
        conn.commit()
        conn.close()

        trend = trend_by_requester(days=30, min_tickets=1, db_path=db_path)
        assert trend == [
            {
                "contact_name": "Nicole McFarland", "contact_email": "n@x.com", "company": "Henssler Financial",
                "ticket_count": 1, "related_ticket_ids": [93980, 94325],
            }
        ]

    def test_merged_away_child_excluded_entirely_not_even_as_related_evidence(self, tmp_path):
        db_path = tmp_path / "index.db"
        conn = connect(db_path)
        _insert_row(conn, 100, "Real Merge Parent", "rm@x.com", has_merged_child_flag=1)
        _insert_row(conn, 101, "Real Merge Parent", "rm@x.com", parent_ticket_id=100)
        conn.commit()
        conn.close()

        trend = trend_by_requester(days=30, min_tickets=1, db_path=db_path)
        assert trend == [
            {
                "contact_name": "Real Merge Parent", "contact_email": "rm@x.com", "company": "Henssler Financial",
                "ticket_count": 1, "related_ticket_ids": [],
            }
        ]


def test_merged_parent_ids_reads_flag_regardless_of_date(tmp_path):
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    _insert_row(conn, 200, "Old Parent", "op@x.com", has_merged_child_flag=1, days_ago=400)
    _insert_row(conn, 201, "Fresh Contact", "fc@x.com", has_merged_child_flag=0, days_ago=1)
    conn.commit()

    assert merged_parent_ids(conn) == {200}
    conn.close()


def test_ensure_schema_migration_is_idempotent_on_existing_v1_database(tmp_path):
    """A database created before the v2 columns existed must gain them on
    the next connect() without erroring or duplicate-adding a column, and
    running ensure_schema() a second time must be a no-op."""
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY, date_entered TEXT NOT NULL DEFAULT '', board TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
            company_id INTEGER, company_name TEXT NOT NULL DEFAULT '', contact_id INTEGER,
            contact_name TEXT NOT NULL DEFAULT '', contact_email TEXT NOT NULL DEFAULT '', indexed_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')")
    conn.commit()
    conn.close()

    conn = connect(db_path)
    columns_after_first = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    assert "parent_ticket_id" in columns_after_first
    assert "resolution_source" in columns_after_first
    version = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()["value"]
    assert version == "2"

    # Second migration pass against the now-current database: no error, no duplicate columns.
    ensure_schema(conn)
    columns_after_second = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    assert columns_after_second == columns_after_first
    conn.close()


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
