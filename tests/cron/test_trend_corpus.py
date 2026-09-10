#!/usr/bin/env python3
"""Tests for cron/trend_corpus.py - the deterministic ticket digest layer."""
from __future__ import annotations

from cron.trend_corpus import (
    TicketDigest,
    build_digests,
    clean_note,
    extract_devices,
    load_corpus,
)
from cron.trend_enrichment_cache import EnrichmentCache

SIGNATURE_BLOCK = """Bitlocker issue on my laptop, please help.

Thanks,
Jane Smith
Director of Operations
Henssler Financial
3735 Cherokee Street
Committed to: Excellence
Direct: [770-555-1212
Main: 770-555-0000
"""

FORWARDED_EMAIL = """From: John Doe
Sent: Monday, August 10, 2026 9:00 AM
To: Help Desk
Subject: FW: Laptop stuck at startup

Please see below, this has been happening for days.
"""


def test_signature_block_is_stripped():
    cleaned = clean_note(SIGNATURE_BLOCK)
    assert "Bitlocker issue" in cleaned
    assert "Henssler Financial" not in cleaned
    assert "Committed to" not in cleaned
    assert "770-555" not in cleaned


def test_forward_at_position_zero_is_not_truncated_to_empty():
    cleaned = clean_note(FORWARDED_EMAIL)
    assert cleaned != ""
    assert "startup" in cleaned.lower()


def test_markdown_image_removed():
    text = "See attached ![\\[image\\]](https://na.myconnectwise.net/foo.png) for details."
    cleaned = clean_note(text)
    assert "myconnectwise" not in cleaned
    assert "See attached" in cleaned
    assert "for details" in cleaned


def test_markdown_link_becomes_label():
    text = "Visit [www.henssler.com](https://www.henssler.com) for more info."
    cleaned = clean_note(text)
    assert "www.henssler.com" in cleaned
    assert "https://www.henssler.com" not in cleaned


def test_clean_note_is_idempotent():
    once = clean_note(SIGNATURE_BLOCK)
    twice = clean_note(once)
    assert once == twice
    forward_once = clean_note(FORWARDED_EMAIL)
    forward_twice = clean_note(forward_once)
    assert forward_once == forward_twice


def test_clean_note_never_raises_on_malformed_input():
    assert clean_note(None) == ""
    assert clean_note("") == ""
    assert clean_note(12345) == ""  # type: ignore[arg-type]
    assert clean_note("   ") == ""


def test_extract_devices_finds_and_dedupes_mixed_case():
    text1 = "Ticket for gwh-pw0ayb5j is failing"
    text2 = "Also affects HPM-SA00E8FQ and gwh-PW0AYB5J again"
    devices = extract_devices(text1, text2)
    assert devices == ["GWH-PW0AYB5J", "HPM-SA00E8FQ"]


def test_build_digests_never_raises_on_malformed_input():
    tickets = [{}, {"id": None}, {"id": 1}, None]
    notes_by_id = {1: [None, {}, {"text": None, "detailDescriptionFlag": True}]}
    time_entries = [None, {}, {"chargeToType": "Project", "chargeToId": 1, "notes": "ignore me"}]
    digests = build_digests(tickets, notes_by_id, time_entries)
    assert len(digests) == 1
    assert digests[0].id == 1
    assert digests[0].tech_notes == []


def test_build_digests_matches_time_entries_by_charge_to_id():
    tickets = [{"id": 100, "summary": "Laptop issue", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {100: []}
    time_entries = [
        {"chargeToId": 100, "chargeToType": "ServiceTicket", "notes": "Ran startup repair", "member": {"name": "Alice"}},
        {"chargeToId": 100, "chargeToType": "Activity", "notes": "should be ignored", "member": {"name": "Bob"}},
        {"chargeToId": 999, "chargeToType": "ServiceTicket", "notes": "wrong ticket", "member": {"name": "Carol"}},
    ]
    digests = build_digests(tickets, notes_by_id, time_entries)
    assert len(digests) == 1
    d = digests[0]
    assert d.tech_notes == ["Ran startup repair"]
    assert d.techs == ["Alice"]


def test_synonym_mapping_startup_repair():
    tickets = [
        {"id": 94792, "summary": "Laptop wont start", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}},
        {"id": 95169, "summary": "PC stuck at boot", "_info": {"dateEntered": "2026-08-02T00:00:00Z"}},
    ]
    notes_by_id = {94792: [], 95169: []}
    time_entries = [
        {"chargeToId": 94792, "chargeToType": "ServiceTicket",
         "notes": "Tried running a startup repair and uninstalling the latest quality update",
         "member": {"name": "Alice"}},
        {"chargeToId": 95169, "chargeToType": "ServiceTicket",
         "notes": "Device stuck in automatic repair loop", "member": {"name": "Bob"}},
    ]
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, time_entries)}
    assert "startup_repair" in digests[94792].entities
    assert "startup_repair" in digests[95169].entities


def test_synonym_mapping_bitlocker_recovery_headline_case():
    """The exact real-world pair the feature exists to catch: tickets 94722 and 95140."""
    tickets = [
        {"id": 94722, "summary": "Bitlocker issue", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}},
        {"id": 95140, "summary": "Laptop problem",
         "_info": {"dateEntered": "2026-08-05T00:00:00Z"}},
    ]
    notes_by_id = {
        94722: [{"text": "User reports Bitlocker issue on laptop", "detailDescriptionFlag": True}],
        95140: [{"text": "Blue screen when turned back on. It's asking for a recovery key.",
                  "detailDescriptionFlag": True}],
    }
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    assert "bitlocker_recovery" in digests[94722].entities
    assert "bitlocker_recovery" in digests[95140].entities


def test_boot_failure_catches_keeps_powering_off():
    """Real ticket 95086 (Gina Auld): zero-entity miss before the fix."""
    tickets = [{"id": 95086, "summary": "Computer issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95086: [{
        "text": "Sorry there is more... And my computer keeps powering off and my "
                "email signature has mysteriously disappeared.",
        "detailDescriptionFlag": True,
    }]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    assert "boot_failure" in digests[95086].entities


def test_update_failure_catches_completed_updates_while_shutting_down():
    """Real ticket 95173 (Belinda Johnson): zero-entity miss before the fix."""
    tickets = [{"id": 95173, "summary": "Laptop issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95173: [{
        "text": "For the past three weeks, I have received the messages below when "
                "turning on my laptop in the morning after it completed updates while "
                "shutting down the day before. This happens on one random day each "
                "week, and the process takes about 30 minutes to cycle through.",
        "detailDescriptionFlag": True,
    }]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    assert "update_failure" in digests[95173].entities


def test_update_failure_catches_tries_to_update_never_successful():
    """Real ticket 95186 (Kent Kissinger): zero-entity miss before the fix."""
    tickets = [{"id": 95186, "summary": "Update issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95186: [{
        "text": "My laptop tries to update almost daily but the updates are never "
                "successful. See attached pic for error message.",
        "detailDescriptionFlag": True,
    }]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    assert "update_failure" in digests[95186].entities


def test_boot_failure_catches_get_computer_to_power_on_and_symptom_words():
    """Real ticket 95420 (Hillary Henry): zero-entity miss before the fix."""
    tickets = [{"id": 95420, "summary": "Please help me get my computer to power on.",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95420: [{
        "text": "User reported tiny white square appeared on screen, followed by "
                "black screen. Device became unresponsive to normal input.",
        "detailDescriptionFlag": True,
    }]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    assert "boot_failure" in digests[95420].entities


def test_boot_failure_and_bitlocker_recovery_stay_distinct():
    tickets = [{"id": 1, "summary": "Bitlocker recovery key needed",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {1: [{"text": "Asking for a recovery key on boot, will not boot past that screen.",
                         "detailDescriptionFlag": True}]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    entities = digests[1].entities
    assert "bitlocker_recovery" in entities
    assert "boot_failure" in entities
    for e in entities:
        assert e not in ("wont_boot",)  # canonical name retired, must not resurface


def test_false_positive_offboarding_shut_off_access():
    tickets = [{"id": 94923, "summary": "Please shut off access for terminated employee",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    digests = {d.id: d for d in build_digests(tickets, {94923: []}, [])}
    entities = digests[94923].entities
    assert "boot_failure" not in entities
    assert "update_failure" not in entities


def test_false_positive_offboarding_shut_off_access_second_ticket():
    tickets = [{"id": 95458, "summary": "Shut off access to shared drive for departing user",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    digests = {d.id: d for d in build_digests(tickets, {95458: []}, [])}
    entities = digests[95458].entities
    assert "boot_failure" not in entities
    assert "update_failure" not in entities


def test_issue_resolution_time_entry_text_fields_populated():
    """issue_text/resolution_text/time_entry_text carry the same content as
    the existing issue/resolution/tech_notes fields, under the names the
    later clustering stage expects. Existing fields must stay intact."""
    tickets = [{"id": 200, "summary": "Bitlocker issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {200: [
        {"text": "User reports Bitlocker prompt on boot", "detailDescriptionFlag": True},
        {"text": "Cleared TPM and resolved", "resolutionFlag": True},
    ]}
    time_entries = [
        {"chargeToId": 200, "chargeToType": "ServiceTicket",
         "notes": "Ran manage-bde -status", "member": {"name": "Alice"}},
    ]
    d = build_digests(tickets, notes_by_id, time_entries)[0]
    assert d.issue == d.issue_text == "User reports Bitlocker prompt on boot"
    assert d.resolution == d.resolution_text == "Cleared TPM and resolved"
    assert d.time_entry_text == "Ran manage-bde -status"
    assert d.tech_notes == ["Ran manage-bde -status"]


def test_time_entry_text_dedupes_repeated_notes():
    tickets = [{"id": 201, "summary": "issue", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    time_entries = [
        {"chargeToId": 201, "chargeToType": "ServiceTicket", "notes": "Rebooted device", "member": {"name": "Alice"}},
        {"chargeToId": 201, "chargeToType": "ServiceTicket", "notes": "Rebooted device", "member": {"name": "Bob"}},
    ]
    d = build_digests(tickets, {201: []}, time_entries)[0]
    assert d.time_entry_text == "Rebooted device"


def test_configurations_normalized_onto_digest():
    tickets = [{"id": 202, "summary": "issue", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    configurations_by_id = {202: [
        {"id": 5, "name": "GWH-PW0AYB5J", "type": {"name": "Laptop"},
         "company": {"name": "Henssler"}, "site": {"name": "Main"}},
    ]}
    d = build_digests(tickets, {202: []}, [], configurations_by_id)[0]
    assert d.configurations == [
        {
            "id": 5, "name": "GWH-PW0AYB5J", "type": "Laptop", "company": "Henssler",
            "site": "Main", "device_identifier": "",
        }
    ]


def test_ticket_with_no_configurations_still_produces_valid_digest():
    """The device-diverse-trend case the owner called out: no shared CI at
    all must not drop the ticket or leave a malformed digest."""
    tickets = [{"id": 203, "summary": "Random device issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    digests = build_digests(tickets, {203: []}, [], {})
    assert len(digests) == 1
    assert digests[0].configurations == []
    assert digests[0].id == 203  # not dropped, not penalized


def test_configurations_malformed_entries_skipped_not_raised():
    tickets = [{"id": 204, "summary": "issue", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    configurations_by_id = {204: [None, {}, {"name": None}, "not a dict"]}
    d = build_digests(tickets, {204: []}, [], configurations_by_id)[0]
    assert d.configurations == []


class _FakeCWClient:
    """Minimal CWClient stand-in for load_corpus() caching tests."""

    def __init__(self):
        self.notes_calls = []
        self.config_calls = []

    def tickets_since(self, since_iso):
        return [{"id": 300, "summary": "issue", "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]

    def notes_for_tickets(self, ticket_ids, **kwargs):
        self.notes_calls.append(list(ticket_ids))
        return {tid: [] for tid in ticket_ids}

    def time_entries_since(self, since_iso):
        return []


def test_load_corpus_second_pass_does_not_refetch_cached_ticket(tmp_path, monkeypatch):
    """Integration-level check for the caching requirement: a second
    load_corpus() call over the same ticket must not call notes_for_tickets
    or configurations_for_tickets again."""
    import cron.trend_corpus_loader as trend_corpus_loader_module

    monkeypatch.setattr(
        trend_corpus_loader_module, "configurations_for_tickets",
        lambda cw, ids, **kw: {tid: [] for tid in ids},
    )
    cache = EnrichmentCache(tmp_path / "cache.json")
    client = _FakeCWClient()

    first = load_corpus(days=14, client=client, cache=cache)
    assert len(first) == 1
    assert client.notes_calls == [[300]]

    second = load_corpus(days=14, client=client, cache=cache)
    assert len(second) == 1
    # No second notes_for_tickets call - the ticket was already cached.
    assert client.notes_calls == [[300]]


def test_false_positive_conference_room_speaker_powering_off():
    tickets = [{"id": 95382, "summary": "Conference room speaker issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95382: [{"text": "The Yamaha speaker in the conference room says Powering Off "
                                     "on its display and won't connect.",
                             "detailDescriptionFlag": True}]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    entities = digests[95382].entities
    assert "boot_failure" not in entities
    assert "update_failure" not in entities


def test_false_positive_printer_crashed_last_week():
    tickets = [{"id": 94741, "summary": "Printer issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {94741: [{"text": "The printer crashed last week and has not printed since.",
                             "detailDescriptionFlag": True}]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    entities = digests[94741].entities
    assert "boot_failure" not in entities
    assert "update_failure" not in entities


def test_false_positive_monitor_driver_crashing_at_startup():
    tickets = [{"id": 95240, "summary": "Monitor issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95240: [{"text": "The monitor driver keeps crashing as soon as it starts up.",
                             "detailDescriptionFlag": True}]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    entities = digests[95240].entities
    assert "boot_failure" not in entities
    assert "update_failure" not in entities


def test_false_positive_authentication_relogin_loop():
    tickets = [{"id": 95635, "summary": "Authentication issue",
                "_info": {"dateEntered": "2026-08-01T00:00:00Z"}}]
    notes_by_id = {95635: [{"text": "User is stuck in an authentication re-login loop after MFA prompt.",
                             "detailDescriptionFlag": True}]}
    digests = {d.id: d for d in build_digests(tickets, notes_by_id, [])}
    entities = digests[95635].entities
    assert "boot_failure" not in entities
    assert "update_failure" not in entities


def test_is_automated_true_for_soc_ticket():
    tickets = [{
        "id": 1,
        "summary": "GWH-PW0AYB5J - Patch management update FAILED to complete. Error: [-3005 Windows communication error]",
        "board": {"name": "Security Operations Center"},
        "_info": {"dateEntered": "2026-08-01T00:00:00Z"},
    }]
    digests = build_digests(tickets, {}, [])
    assert digests[0].is_automated is True


def test_is_automated_false_for_triage_ticket():
    tickets = [{
        "id": 2,
        "summary": "Laptop stuck at startup",
        "board": {"name": "Triage"},
        "_info": {"dateEntered": "2026-08-01T00:00:00Z"},
    }]
    digests = build_digests(tickets, {}, [])
    assert digests[0].is_automated is False


def test_ticket_digest_is_a_dataclass_with_expected_fields():
    d = TicketDigest(
        id=1, date="2026-08-01", board="Triage", summary="s", contact="c",
        status="New", priority="Priority 3 - Medium", issue="i", resolution="r",
    )
    assert d.tech_notes == []
    assert d.techs == []
    assert d.devices == []
    assert d.entities == set()
    assert d.is_automated is False
