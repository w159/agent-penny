"""Tests for cron/ticket_context_enrichment.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import cron.ticket_context_enrichment as tce
from cron.cw_contact_index import connect


class _FakeCWClient:
    """Duck-typed CW client stub: .get() answers /service/tickets/{id} by
    id, .notes_for_tickets() answers by id. No network, no CWClient
    construction (which requires real .env credentials)."""

    def __init__(self, tickets_by_id: dict, notes_by_id: dict | None = None):
        self._tickets_by_id = tickets_by_id
        self._notes_by_id = notes_by_id or {}

    def get(self, path, **_params):
        ticket_id = int(path.rstrip("/").rsplit("/", 1)[-1])
        return self._tickets_by_id[ticket_id]

    def notes_for_tickets(self, ticket_ids, **_kwargs):
        return {tid: self._notes_by_id.get(tid, []) for tid in ticket_ids}


def _base_ticket(ticket_id, **overrides):
    ticket = {
        "id": ticket_id,
        "owner": {"id": 167, "identifier": "evelarde", "name": "Ernesto Velarde"},
        "contactPhoneNumber": "6787973715",
        "closedFlag": False,
        "closedDate": None,
        "dateResolved": None,
        "parentTicketId": None,
        "hasChildTicket": False,
        "hasMergedChildTicketFlag": False,
    }
    ticket.update(overrides)
    return ticket


def _seed_index_row(db_path, ticket_id, *, date_entered=None, context_enriched_at=None):
    conn = connect(db_path)
    entered = date_entered or (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO tickets (id, date_entered, contact_name, contact_email, company_name, "
        "context_enriched_at, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, entered, "Some Contact", "sc@x.com", "Henssler Financial", context_enriched_at, entered),
    )
    conn.commit()
    conn.close()


def test_due_ticket_ids_includes_never_enriched_and_excludes_recently_enriched(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    fresh_iso = datetime.now(timezone.utc).isoformat()
    stale_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    _seed_index_row(db_path, 1, context_enriched_at=None)
    _seed_index_row(db_path, 2, context_enriched_at=fresh_iso)
    _seed_index_row(db_path, 3, context_enriched_at=stale_iso)

    due = tce.due_ticket_ids(stale_after_days=14, db_path=db_path)

    assert set(due) == {1, 3}


def test_enrich_tickets_writes_owner_configs_issue_resolution_and_linkage_fields(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    _seed_index_row(db_path, 92827)

    ticket = _base_ticket(
        92827, hasChildTicket=True, hasMergedChildTicketFlag=False, parentTicketId=None,
    )
    notes = [
        {"text": "User cannot access Outlook, access denied error.", "detailDescriptionFlag": True},
        {"text": "Reset the account permissions in Exchange admin center.", "resolutionFlag": True},
    ]
    client = _FakeCWClient({92827: ticket}, {92827: notes})
    monkeypatch.setattr(
        tce, "configurations_for_tickets",
        lambda _client, _ids, **_kw: {92827: [{"id": 5, "deviceIdentifier": "GWH-ABC", "_info": {"name": "GWH-ABC"}}]},
    )

    summary = tce.enrich_tickets([92827], client=client, db_path=db_path)

    assert summary["tickets_enriched"] == 1
    assert summary["tickets_not_found_in_index"] == 0
    assert summary["resolution_source_counts"]["resolutionFlag"] == 1

    conn = connect(db_path)
    row = dict(conn.execute("SELECT * FROM tickets WHERE id = 92827").fetchone())
    conn.close()

    assert row["owner_name"] == "Ernesto Velarde"
    assert row["owner_identifier"] == "evelarde"
    assert row["contact_phone"] == "6787973715"
    assert row["issue_text"] == "User cannot access Outlook, access denied error."
    assert row["resolution_text"] == "Reset the account permissions in Exchange admin center."
    assert row["resolution_source"] == "resolutionFlag"
    assert row["has_child_ticket"] == 1
    assert row["has_merged_child_flag"] == 0
    assert row["parent_ticket_id"] is None
    assert json.loads(row["configurations"]) == [
        {"id": 5, "name": "GWH-ABC", "type": "", "company": "", "site": "", "device_identifier": "GWH-ABC"}
    ]
    assert row["context_enriched_at"] is not None


def test_resolution_source_is_heuristic_when_no_resolution_flag_present(tmp_path, monkeypatch):
    """Real 2026-09-22 finding: this tenant almost never sets resolutionFlag.
    A ticket with a real last note but no resolutionFlag note must be
    marked heuristic, never silently presented as CW's own resolution."""
    db_path = tmp_path / "index.db"
    _seed_index_row(db_path, 90653)

    ticket = _base_ticket(90653, parentTicketId=88788, closedFlag=True, closedDate="2026-07-22T13:22:21.103Z")
    notes = [
        {"text": "Client reports Tamarac won't load.", "detailDescriptionFlag": True},
        {"text": "Escalating to network team, still investigating.", },
    ]
    client = _FakeCWClient({90653: ticket}, {90653: notes})
    monkeypatch.setattr(tce, "configurations_for_tickets", lambda _client, _ids, **_kw: {})

    tce.enrich_tickets([90653], client=client, db_path=db_path)

    conn = connect(db_path)
    row = dict(conn.execute("SELECT * FROM tickets WHERE id = 90653").fetchone())
    conn.close()

    assert row["resolution_source"] == "heuristic_last_note"
    assert row["resolution_text"] == "Escalating to network team, still investigating."
    assert row["parent_ticket_id"] == 88788
    assert row["closed_flag"] == 1


def test_resolution_source_is_none_when_no_notes_carry_resolution_text(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    _seed_index_row(db_path, 5)
    ticket = _base_ticket(5)
    # Only the issue note exists -- no resolution and no fallback text either.
    notes = [{"text": "User cannot print.", "detailDescriptionFlag": True}]
    client = _FakeCWClient({5: ticket}, {5: notes})
    monkeypatch.setattr(tce, "configurations_for_tickets", lambda _client, _ids, **_kw: {})

    tce.enrich_tickets([5], client=client, db_path=db_path)

    conn = connect(db_path)
    row = dict(conn.execute("SELECT * FROM tickets WHERE id = 5").fetchone())
    conn.close()

    assert row["resolution_text"] == ""
    assert row["resolution_source"] == "none"


def test_enrich_tickets_never_clobbers_base_columns_owned_by_refresh_index(tmp_path, monkeypatch):
    """enrich_tickets writes via UPDATE, never INSERT OR REPLACE -- the base
    contact/company/status columns refresh_index() owns must survive an
    enrichment pass untouched."""
    db_path = tmp_path / "index.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO tickets (id, date_entered, board, status, contact_name, contact_email, "
        "company_name, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (10, "2026-08-01T00:00:00Z", "Triage", "Open", "Erica Martin", "e@x.com",
         "Henssler Financial", "2026-08-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    client = _FakeCWClient({10: _base_ticket(10)}, {10: []})
    monkeypatch.setattr(tce, "configurations_for_tickets", lambda _client, _ids, **_kw: {})

    tce.enrich_tickets([10], client=client, db_path=db_path)

    conn = connect(db_path)
    row = dict(conn.execute("SELECT * FROM tickets WHERE id = 10").fetchone())
    conn.close()

    assert row["board"] == "Triage"
    assert row["status"] == "Open"
    assert row["contact_name"] == "Erica Martin"
    assert row["company_name"] == "Henssler Financial"


def test_enrich_tickets_counts_ids_not_present_in_local_index_without_erroring(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"  # empty index -- ticket id never seen by refresh_index
    client = _FakeCWClient({999: _base_ticket(999)}, {999: []})
    monkeypatch.setattr(tce, "configurations_for_tickets", lambda _client, _ids, **_kw: {})

    summary = tce.enrich_tickets([999], client=client, db_path=db_path)

    assert summary["tickets_enriched"] == 0
    assert summary["tickets_not_found_in_index"] == 1


def test_enrich_tickets_empty_input_is_a_clean_noop(tmp_path):
    summary = tce.enrich_tickets([], db_path=tmp_path / "index.db")
    assert summary == {
        "tickets_requested": 0,
        "tickets_enriched": 0,
        "tickets_not_found_in_index": 0,
        "resolution_source_counts": {},
        "enriched_at": None,
    }


def test_run_nightly_enrichment_only_processes_due_tickets(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    fresh_iso = datetime.now(timezone.utc).isoformat()
    _seed_index_row(db_path, 1, context_enriched_at=None)
    _seed_index_row(db_path, 2, context_enriched_at=fresh_iso)

    seen_ids = []

    def _fake_enrich(ticket_ids, *, client=None, db_path=None):
        seen_ids.extend(ticket_ids)
        return {"tickets_requested": len(ticket_ids), "tickets_enriched": len(ticket_ids),
                "tickets_not_found_in_index": 0, "resolution_source_counts": {}, "enriched_at": "now"}

    monkeypatch.setattr(tce, "enrich_tickets", _fake_enrich)

    tce.run_nightly_enrichment(stale_after_days=14, db_path=db_path)

    assert seen_ids == [1]
