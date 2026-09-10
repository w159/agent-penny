"""
Unit tests for cron/triage_nag.py — selection/decision engine for Penny's
proactive Triage-board nagging. Fixture ticket dicts only, no live CW
calls; ConnectWise is mocked everywhere.

The critical cases:
  - the `_info.dateEntered` trap (see cron/triage_nag.py's
    ticket_entered_at): reading the wrong key silently ages every ticket
    to epoch, which would make everything fire "furious" immediately.
  - probe_tech_availability never returns a tech on ambiguous data - a
    false "he's free" claim burns the feature's credibility permanently.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cron.triage_nag import (
    MAX_NAGS_PER_RUN,
    MIN_RENAG_MINUTES,
    assess_blocking,
    is_human_ticket,
    probe_tech_availability,
    select_triage_nags,
    tier_for,
    ticket_entered_at,
)

NOW = datetime(2026, 8, 25, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state_file = tmp_path / "triage_nag_state.json"
    monkeypatch.setattr("cron.triage_nag.OPS_DIR", tmp_path)
    monkeypatch.setattr("cron.triage_nag.STATE_FILE", state_file)
    return state_file


def make_ticket(
    ticket_id=1,
    summary="Cannot log in to workstation",
    priority="Priority 3 - Medium",
    entered_at=None,
    entered_at_info_only=True,
    contact_name="Jane Doe",
    owner="",
    last_note_at=None,
):
    ticket = {
        "id": ticket_id,
        "summary": summary,
        "priority": {"name": priority},
        "contact": {"name": contact_name},
        "owner": owner,
        "board": {"name": "Triage"},
    }
    if entered_at is not None:
        iso = entered_at.isoformat()
        if entered_at_info_only:
            ticket["_info"] = {"dateEntered": iso}
        else:
            ticket["dateEntered"] = iso
    if last_note_at:
        ticket["_last_note_at"] = last_note_at
    return ticket


_DEFAULT_MEMBERS = [
    {"id": 176, "firstName": "Jarvis", "lastName": "Williams", "inactiveFlag": False},
    {"id": 167, "firstName": "Ernesto", "lastName": "Velarde", "inactiveFlag": False},
]


class FakeCWClient:
    """Duck-typed stand-in for cron.cw_client.CWClient. No network calls.

    /system/members defaults to a roster that verifies both TECH_ROSTER
    entries cleanly, so tests that don't care about roster verification
    (nearly all of them) never have to think about it. Pass `members=`
    to exercise a mismatch/missing/inactive member.
    """

    def __init__(self, schedule_entries=None, time_entries=None, members=None):
        self._schedule_entries = schedule_entries or {}
        self._time_entries = time_entries or {}
        self._members = _DEFAULT_MEMBERS if members is None else members

    def get(self, path, **params):
        if path == "/system/members":
            return self._members
        if path == "/schedule/entries":
            conditions = params.get("conditions", "")
            for member_id, entries in self._schedule_entries.items():
                if f"member/id={member_id}" in conditions:
                    if "dateStart>" in conditions:
                        # "next free time" lookup - only future entries.
                        return [e for e in entries if e.get("_future")]
                    return [e for e in entries if not e.get("_future")]
            return []
        raise AssertionError(f"unexpected GET {path}")

    def paged(self, path, conditions, page_size=1000):
        if path == "/time/entries":
            for member_id, entries in self._time_entries.items():
                if f"member/id={member_id}" in conditions:
                    return entries
            return []
        if path == "/schedule/entries":
            for member_id, entries in self._schedule_entries.items():
                if f"member/id={member_id}" in conditions:
                    return [e for e in entries if not e.get("_future")]
            return []
        raise AssertionError(f"unexpected paged {path}")


# ---------------------------------------------------------------------------
# ticket_entered_at - the _info trap
# ---------------------------------------------------------------------------


class TestTicketEnteredAt:
    def test_reads_from_info_when_top_level_empty(self):
        entered = NOW - timedelta(hours=2)
        ticket = make_ticket(entered_at=entered, entered_at_info_only=True)
        assert ticket.get("dateEntered") is None
        result = ticket_entered_at(ticket)
        assert result == entered

    def test_falls_back_to_top_level_when_info_missing(self):
        entered = NOW - timedelta(hours=1)
        ticket = make_ticket(entered_at=entered, entered_at_info_only=False)
        result = ticket_entered_at(ticket)
        assert result == entered

    def test_missing_in_both_places_returns_none(self):
        ticket = make_ticket(entered_at=None)
        assert ticket_entered_at(ticket) is None


# ---------------------------------------------------------------------------
# assess_blocking
# ---------------------------------------------------------------------------


class TestAssessBlocking:
    def test_blocking_phrase_in_summary(self):
        ticket = make_ticket(summary="User is locked out of their account", priority="Priority 4 - Low")
        blocking, impact = assess_blocking(ticket)
        assert blocking is True
        assert "locked out" in impact.lower()

    def test_high_priority_without_phrase_is_blocking(self):
        ticket = make_ticket(summary="General question about a report", priority="Priority 1 - Emergency")
        blocking, _ = assess_blocking(ticket)
        assert blocking is True

    def test_neither_phrase_nor_priority_is_not_blocking(self):
        ticket = make_ticket(summary="General question about a report", priority="Priority 4 - Low")
        blocking, _ = assess_blocking(ticket)
        assert blocking is False


# ---------------------------------------------------------------------------
# tier_for - every boundary
# ---------------------------------------------------------------------------


class TestTierForBlocking:
    @pytest.mark.parametrize("age,expected", [
        (0, None),
        (29, None),
        (30, "poke"),
        (59, "poke"),
        (60, "named"),
        (119, "named"),
        (120, "loud"),
        (239, "loud"),
        (240, "furious"),
        (10_000, "furious"),
    ])
    def test_boundaries(self, age, expected):
        assert tier_for(age, blocking=True, prior_nag_count=0) == expected


class TestTierForNonBlocking:
    @pytest.mark.parametrize("age,expected", [
        (0, None),
        (119, None),
        (120, "poke"),
        (239, "poke"),
        (240, "named"),
        (479, "named"),
        (480, "loud"),
        (1439, "loud"),
        (1440, "furious"),
        (100_000, "furious"),
    ])
    def test_boundaries(self, age, expected):
        assert tier_for(age, blocking=False, prior_nag_count=0) == expected


# ---------------------------------------------------------------------------
# Machine-noise exclusion (reuses cron.outage_triage.route_ticket)
# ---------------------------------------------------------------------------


class TestMachineNoiseExclusion:
    # NOC Checks (AM/PM) and ThreatLocker are deliberately NOT used as
    # machine-noise examples here: cron/outage_triage.NEVER_MOVE_SHAPES
    # (Jerry, 2026-08-25) documents both as genuine help desk work that a
    # prior routing pass wrongly swept off Triage - route_ticket correctly
    # returns ACTION_LEAVE for them, i.e. they ARE selectable as human
    # tickets. Reusing the classifier means trusting that correction, not
    # re-deriving a different answer for the same shapes.
    def test_infrastructure_device_alert_never_selected(self):
        ticket = make_ticket(
            summary="pa-820-01 on Henssler Financial Headquarters (hensslerhq): This network element has gone offline",
            contact_name="Auvik Alert",
            entered_at=NOW - timedelta(hours=1),
        )
        assert is_human_ticket(ticket) is False

    def test_machine_contact_marker_never_selected(self):
        ticket = make_ticket(
            summary="NinjaOne Monitoring: Device CPU threshold exceeded on WKS-042",
            contact_name="NinjaOne Notifications",
            entered_at=NOW - timedelta(hours=1),
        )
        assert is_human_ticket(ticket) is False

    def test_ordinary_human_ticket_is_selected(self):
        ticket = make_ticket(summary="Cannot log in to workstation", contact_name="Jane Doe", entered_at=NOW - timedelta(hours=1))
        assert is_human_ticket(ticket) is True

    def test_noc_checks_shape_is_treated_as_human_work(self):
        """Regression guard for NEVER_MOVE_SHAPES: this shape must stay
        selectable, not be excluded as noise."""
        ticket = make_ticket(summary="NOC Checks (AM/PM)", contact_name="", entered_at=NOW - timedelta(hours=1))
        assert is_human_ticket(ticket) is True


# ---------------------------------------------------------------------------
# probe_tech_availability - the highest-risk piece
# ---------------------------------------------------------------------------


class TestProbeTechAvailability:
    def test_busy_tech_with_no_hands_off_proof_is_omitted(self):
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
            },
            time_entries={
                176: [{"ticket": {"id": 111}, "status": {"name": "In Progress"}, "summary": "",
                      "timeStart": NOW.isoformat()}],
                167: [{"ticket": {"id": 112}, "status": {"name": "In Progress"}, "summary": "",
                      "timeStart": NOW.isoformat()}],
            },
        )
        result = probe_tech_availability(client, NOW)
        assert result == ()

    def test_no_calendar_entry_proves_free(self):
        client = FakeCWClient(schedule_entries={176: [], 167: [
            {"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
             "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False},
        ]}, time_entries={167: []})
        result = probe_tech_availability(client, NOW)
        names = [t.name for t in result]
        assert "Jarvis Williams" in names
        assert "Ernesto Velarde" not in names
        jarvis = next(t for t in result if t.name == "Jarvis Williams")
        assert "calendar" in jarvis.reason.lower()

    def test_hands_off_time_entry_proves_free(self):
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [],
            },
            time_entries={
                176: [{"ticket": {"id": 96108}, "status": {"name": "Waiting Client Response"}, "summary": "",
                      "timeStart": NOW.isoformat()}],
            },
        )
        result = probe_tech_availability(client, NOW)
        names = [t.name for t in result]
        assert "Jarvis Williams" in names
        jarvis = next(t for t in result if t.name == "Jarvis Williams")
        assert "96108" in jarvis.reason
        assert "waiting" in jarvis.reason.lower()

    def test_summary_marker_hands_off_proof(self):
        # Status is a NON-active hands-off-adjacent status ("Scheduled"),
        # not "In Progress" - an active status always overrides a summary
        # marker (see test_active_status_overrides_summary_marker below),
        # so this proves the summary marker alone can only ever carry the
        # decision when the status itself isn't actively-working.
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [],
            },
            time_entries={
                176: [{"ticket": {"id": 500}, "status": {"name": "Scheduled"},
                      "summary": "Windows imaging install in progress", "timeStart": NOW.isoformat()}],
            },
        )
        result = probe_tech_availability(client, NOW)
        jarvis = next(t for t in result if t.name == "Jarvis Williams")
        assert "500" in jarvis.reason

    def test_active_status_overrides_summary_marker(self):
        """Defect-1 regression: an actively-worked ticket must never be
        read as hands-off just because its summary contains a marker
        word. The user's own complaint sentence can say "sync" while the
        tech is actively troubleshooting with her right now."""
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
            },
            time_entries={
                176: [{"ticket": {"id": 900}, "status": {"name": "In Progress"},
                      "summary": "User reports she cannot sync mail on her iPhone, actively "
                                 "troubleshooting with her now", "timeStart": NOW.isoformat()}],
                167: [{"ticket": {"id": 901}, "status": {"name": "In Progress"},
                      "summary": "please give me an update on the migration status call",
                      "timeStart": NOW.isoformat()}],
            },
        )
        result = probe_tech_availability(client, NOW)
        assert result == ()

    @pytest.mark.parametrize("marker", ["install", "imaging", "reboot", "patch"])
    def test_each_retained_summary_marker_omitted_under_active_status(self, marker):
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [],
            },
            time_entries={
                176: [{"ticket": {"id": 501}, "status": {"name": "In Progress"},
                      "summary": f"Running the {marker} now, watching it", "timeStart": NOW.isoformat()}],
            },
        )
        result = probe_tech_availability(client, NOW)
        assert not any(t.name == "Jarvis Williams" for t in result)

    def test_second_active_ticket_omits_tech_and_reason_never_claims_only_falsely(self):
        """Defect-2 regression: a second, actively-in-progress ticket
        logged the same day must omit the tech outright - the hands-off
        entry being most recent does not mean it is the tech's only open
        work."""
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [],
            },
            time_entries={
                176: [
                    {"ticket": {"id": 700}, "status": {"name": "In Progress"},
                     "summary": "Rebuilding profile after corruption",
                     "timeStart": (NOW - timedelta(hours=3)).isoformat()},
                    {"ticket": {"id": 701}, "status": {"name": "Waiting Client Response"},
                     "summary": "", "timeStart": (NOW - timedelta(minutes=5)).isoformat()},
                ],
            },
        )
        result = probe_tech_availability(client, NOW)
        assert not any(t.name == "Jarvis Williams" for t in result)

    def test_reason_says_only_solely_when_proven(self):
        """A second open ticket that is itself hands-off (not active)
        must not be hidden behind the word "only" - the reason must
        state only what was proven."""
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [],
            },
            time_entries={
                176: [
                    {"ticket": {"id": 702}, "status": {"name": "Waiting Client Response"},
                     "summary": "", "timeStart": (NOW - timedelta(hours=2)).isoformat()},
                    {"ticket": {"id": 703}, "status": {"name": "Scheduled"},
                     "summary": "", "timeStart": (NOW - timedelta(minutes=5)).isoformat()},
                ],
            },
        )
        result = probe_tech_availability(client, NOW)
        jarvis = next(t for t in result if t.name == "Jarvis Williams")
        assert "only" not in jarvis.reason.lower()
        assert "703" in jarvis.reason

    def test_busy_entry_past_first_schedule_page_is_seen(self):
        """Defect-3 regression: _schedule_busy_now must page through
        /schedule/entries like its sibling _hands_off_time_entry_reason,
        not stop at the first 200-row page."""
        filler = [
            {"dateStart": (NOW + timedelta(days=1, hours=i)).isoformat(),
             "dateEnd": (NOW + timedelta(days=1, hours=i, minutes=30)).isoformat()}
            for i in range(250)
        ]
        busy_now_entry = {
            "dateStart": (NOW - timedelta(minutes=15)).isoformat(),
            "dateEnd": (NOW + timedelta(minutes=15)).isoformat(),
        }
        client = FakeCWClient(schedule_entries={176: filler + [busy_now_entry], 167: []})
        result = probe_tech_availability(client, NOW)
        assert not any(t.name == "Jarvis Williams" for t in result)

    def test_roster_name_mismatch_omits_tech(self):
        client = FakeCWClient(
            schedule_entries={176: [], 167: []},
            members=[
                {"id": 176, "firstName": "Someone", "lastName": "Else", "inactiveFlag": False},
                {"id": 167, "firstName": "Ernesto", "lastName": "Velarde", "inactiveFlag": False},
            ],
        )
        result = probe_tech_availability(client, NOW)
        names = [t.name for t in result]
        assert "Jarvis Williams" not in names
        assert "Ernesto Velarde" in names

    def test_member_lookup_failure_omits_all_techs(self):
        class RaisingMembersClient(FakeCWClient):
            def get(self, path, **params):
                if path == "/system/members":
                    raise RuntimeError("CW unavailable")
                return super().get(path, **params)

        client = RaisingMembersClient(schedule_entries={176: [], 167: []})
        result = probe_tech_availability(client, NOW)
        assert result == ()

    def test_naive_and_aware_schedule_datetimes_compare_correctly(self):
        """Schedule entries stored without a timezone suffix must still
        compare correctly against an aware `now`, not raise or silently
        mis-omit."""
        client = FakeCWClient(schedule_entries={
            176: [{"dateStart": (NOW - timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
                  "dateEnd": (NOW + timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
                  "doneFlag": False}],
            167: [],
        }, time_entries={176: [{"ticket": {"id": 999}, "status": {"name": "In Progress"}, "summary": "",
                                "timeStart": NOW.isoformat()}]})
        result = probe_tech_availability(client, NOW)
        assert not any(t.name == "Jarvis Williams" for t in result)

    def test_ambiguous_data_never_included(self):
        # Busy, and the only time entry has no hands-off status or summary
        # marker at all - must be omitted, not guessed at.
        client = FakeCWClient(
            schedule_entries={
                176: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
                167: [{"dateStart": (NOW - timedelta(minutes=30)).isoformat(),
                      "dateEnd": (NOW + timedelta(minutes=30)).isoformat(), "doneFlag": False}],
            },
            time_entries={
                176: [{"ticket": {"id": 777}, "status": {"name": "New"}, "summary": "Setting up a new laptop",
                      "timeStart": NOW.isoformat()}],
                167: [],
            },
        )
        result = probe_tech_availability(client, NOW)
        assert result == ()


# ---------------------------------------------------------------------------
# select_triage_nags - state, cap, ordering
# ---------------------------------------------------------------------------


def _idle_client():
    return FakeCWClient(schedule_entries={176: [], 167: []}, time_entries={})


class TestSelectTriageNags:
    def test_empty_input_produces_no_messages(self):
        result = select_triage_nags([], _idle_client(), NOW)
        assert result.silent is True
        assert result.fired == []

    def test_below_threshold_stays_silent(self):
        ticket = make_ticket(summary="General question", priority="Priority 4 - Low",
                              entered_at=NOW - timedelta(minutes=10))
        result = select_triage_nags([ticket], _idle_client(), NOW)
        assert result.silent is True

    def test_blocking_ticket_over_threshold_fires(self):
        ticket = make_ticket(summary="Cannot log in", priority="Priority 3 - Medium",
                              entered_at=NOW - timedelta(minutes=45))
        result = select_triage_nags([ticket], _idle_client(), NOW)
        assert result.silent is False
        assert result.fired[0].tier == "poke"

    def test_renag_suppressed_within_min_renag_minutes(self):
        ticket = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=45))
        select_triage_nags([ticket], _idle_client(), NOW)  # first run, fires "poke"
        later = NOW + timedelta(minutes=10)
        ticket2 = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=45))
        result = select_triage_nags([ticket2], _idle_client(), later)
        assert result.silent is True

    def test_escalation_fires_even_within_cooldown(self):
        ticket = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=45))
        select_triage_nags([ticket], _idle_client(), NOW)  # "poke"
        later = NOW + timedelta(minutes=20)
        ticket2 = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=65))
        result = select_triage_nags([ticket2], _idle_client(), later)
        assert result.silent is False
        assert result.fired[0].tier == "named"

    def test_renag_allowed_after_cooldown(self):
        ticket = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=45))
        select_triage_nags([ticket], _idle_client(), NOW)  # "poke"
        later = NOW + timedelta(minutes=MIN_RENAG_MINUTES + 5)
        ticket2 = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=50))
        result = select_triage_nags([ticket2], _idle_client(), later)
        assert result.silent is False

    def test_owner_and_new_note_resets_and_silences(self):
        ticket = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=45),
                              last_note_at="2026-08-25T10:00:00+00:00")
        select_triage_nags([ticket], _idle_client(), NOW)
        later = NOW + timedelta(minutes=60)
        ticket2 = make_ticket(summary="Cannot log in", entered_at=NOW - timedelta(minutes=105),
                               owner="Jarvis Williams", last_note_at="2026-08-25T18:30:00+00:00")
        result = select_triage_nags([ticket2], _idle_client(), later)
        assert result.silent is True

    def test_machine_noise_excluded_from_selection(self):
        ticket = make_ticket(
            summary="pa-820-01 on Henssler Financial Headquarters (hensslerhq): This network element has gone offline",
            contact_name="Auvik Alert",
            entered_at=NOW - timedelta(hours=5),
        )
        result = select_triage_nags([ticket], _idle_client(), NOW)
        assert result.silent is True

    def test_max_nags_per_run_cap_and_worst_first(self):
        tickets = [
            make_ticket(ticket_id=1, summary="Cannot log in", entered_at=NOW - timedelta(minutes=45)),  # poke
            make_ticket(ticket_id=2, summary="Cannot log in", entered_at=NOW - timedelta(minutes=65)),  # named
            make_ticket(ticket_id=3, summary="Cannot log in", entered_at=NOW - timedelta(minutes=125)),  # loud
            make_ticket(ticket_id=4, summary="Cannot log in", entered_at=NOW - timedelta(minutes=245)),  # furious
        ]
        result = select_triage_nags(tickets, _idle_client(), NOW)
        assert len(result.fired) == MAX_NAGS_PER_RUN
        tiers_in_order = [c.tier for c in result.fired]
        assert tiers_in_order == ["furious", "loud", "named"]
        assert result.deferred_count == 1

    def test_departed_ticket_pruned_from_state(self):
        import json
        ticket = make_ticket(ticket_id=9, summary="Cannot log in", entered_at=NOW - timedelta(minutes=45))
        select_triage_nags([ticket], _idle_client(), NOW)
        from cron.triage_nag import STATE_FILE
        assert "9" in json.loads(STATE_FILE.read_text())
        result = select_triage_nags([], _idle_client(), NOW + timedelta(hours=1))
        assert "9" not in json.loads(STATE_FILE.read_text())
        assert result.silent is True

# build_fact_block() and build_triage_nag_prompt_block() and their tests
# were removed 2026-09-09 along with the content-free nag prose path they
# fed (see cron/triage_nag.py's module docstring). select_triage_nags()
# above remains covered; agent-penny-assist owns its own tests for how it
# consumes NagFacts/NagCandidate/TriageNagResult/TechAvailability.
