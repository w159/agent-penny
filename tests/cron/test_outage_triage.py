#!/usr/bin/env python3
"""Tests for cron/outage_triage.py - deciding which Triage tickets are NOC
noise, and where each one belongs.

The distinction this module exists to make, and the one the earlier phases got
wrong by conflating: whether a ticket should LEAVE Triage is a different
question from whether Penny TRACKS it as an outage.

Measured on live Triage, 13 of 60 open tickets were machine-generated NOC or
SOC output. Only 5 of those were outages against services Henssler tracks. If
the move decision were gated on the tracking decision, the Brivo, Sentry and
Defender tickets would sit on Triage forever, which is the exact noise problem
this is meant to solve.

Destination is learned from the boards' own history, not asserted. Over 90
days, Microsoft 365 Defender filed 620 tickets to SOC against 18 to NOC, while
the status feed filed 952 to NOC against 44 to SOC.
"""
from __future__ import annotations

from datetime import datetime, timezone

from cron.outage_inventory import build_registry
from cron.outage_triage import (
    ACTION_LEAVE,
    learn_shape_routing,
    summary_shape,
    ACTION_MOVE_AND_CLOSE,
    ACTION_MOVE_AND_TRACK,
    BOARD_NOC,
    BOARD_SOC,
    learn_routing,
    route_ticket,
)

NOW = datetime(2026, 8, 25, tzinfo=timezone.utc)


def _config(cfg_id, name, type_name="Software"):
    return {"id": cfg_id, "name": name, "type": {"name": type_name},
            "site": {"name": "HQ"}, "status": {"name": "Active"}}


REGISTRY = build_registry([
    _config(1, "Microsoft Office 365 - Azure/Exchange/Defender/Entra ID/Active Directory/Sync/SSO/IDP",
            "Vendor-Office365"),
    _config(2, "NinjaOne"),
])


def _ticket(ticket_id, summary, contact=None, board="Triage", entered="2026-08-25T09:00:00Z"):
    return {"id": ticket_id, "summary": summary,
            "contact": ({"name": contact} if contact else None),
            "board": {"name": board}, "_info": {"dateEntered": entered}}


# --------------------------------------------------------------------------
# learning the destination from history
# --------------------------------------------------------------------------

def test_routing_is_learned_from_where_tickets_actually_went():
    history = (
        [_ticket(i, "x", contact="Microsoft 365 Defender", board=BOARD_SOC) for i in range(620)]
        + [_ticket(i, "x", contact="Microsoft 365 Defender", board=BOARD_NOC) for i in range(18)]
        + [_ticket(i, "x", contact="notifications", board=BOARD_NOC) for i in range(952)]
        + [_ticket(i, "x", contact="notifications", board=BOARD_SOC) for i in range(44)]
    )
    routing = learn_routing(history)
    assert routing["microsoft 365 defender"] == BOARD_SOC
    assert routing["notifications"] == BOARD_NOC


def test_a_contact_with_no_clear_majority_is_not_learned():
    """An even split is not a policy. Leaving it out forces the structural
    rules to decide instead of coin-flipping a destination."""
    history = ([_ticket(i, "x", contact="ambiguous", board=BOARD_SOC) for i in range(50)]
               + [_ticket(i, "x", contact="ambiguous", board=BOARD_NOC) for i in range(50)])
    assert "ambiguous" not in learn_routing(history)


def test_low_volume_contacts_are_not_learned():
    history = [_ticket(1, "x", contact="rare", board=BOARD_SOC)]
    assert "rare" not in learn_routing(history)


def test_human_contacts_never_become_routing_rules():
    """Real people file real tickets to Triage. A person who once had a ticket
    land on NOC must not turn into a routing rule."""
    history = [_ticket(i, "x", contact="Marsha Brooks", board=BOARD_SOC) for i in range(30)]
    routing = learn_routing(history, known_machine_contacts={"notifications"})
    assert "marsha brooks" not in routing


# --------------------------------------------------------------------------
# the move decision is broader than the tracking decision
# --------------------------------------------------------------------------

ROUTING = {"notifications": BOARD_NOC, "microsoft 365 defender": BOARD_SOC,
           "help desk": BOARD_NOC, "auvik system": BOARD_SOC}


def test_tracked_outage_moves_and_is_tracked():
    """Live ticket 96184."""
    decision = route_ticket(
        _ticket(96184, "SP1461217: SharePoint Online Service Health Incident (Service Degradation)"),
        REGISTRY, ROUTING)
    assert decision.action == ACTION_MOVE_AND_TRACK
    assert decision.destination == BOARD_NOC
    assert decision.service_key == "sharepoint_online"


def test_untracked_vendor_outage_still_leaves_triage():
    """Live tickets 96171 and 96183. Brivo and Sentry are not services
    Henssler runs, so Penny does not track them - but they are still machine
    noise on the help desk board and must go."""
    for ticket_id, summary in ((96171, "\U0001F534 Brivo is having a MAJOR outage"),
                               (96183, "\U0001F7E0 Sentry is having a MINOR outage")):
        decision = route_ticket(_ticket(ticket_id, summary, contact="notifications"),
                                REGISTRY, ROUTING)
        assert decision.action == ACTION_MOVE_AND_CLOSE, summary
        assert decision.destination == BOARD_NOC, summary
        assert "not tracked" in decision.reason.lower(), summary


def test_security_source_goes_to_soc_not_noc():
    """Live tickets 96164 and 96169."""
    decision = route_ticket(
        _ticket(96164, "Threat analytics report from Microsoft 365 Defender",
                contact="Microsoft 365 Defender"), REGISTRY, ROUTING)
    assert decision.action == ACTION_MOVE_AND_CLOSE
    assert decision.destination == BOARD_SOC


def test_noc_checks_stay_on_triage_despite_the_name_and_the_history():
    """Regression, live ticket 96179. Penny moved this and should not have.
    The name says NOC and the board history agreed 83 to 1; both were wrong.
    It is help desk work, not monitoring output."""
    decision = route_ticket(_ticket(96179, "NOC Checks (AM/PM)", contact="Help Desk"),
                            REGISTRY, ROUTING, {summary_shape("NOC Checks (AM/PM)"): BOARD_NOC})
    assert decision.action == ACTION_LEAVE


def test_any_threatlocker_ticket_stays_on_triage():
    """Regression, live ticket 96190. Requests and approvals are human
    workflow. History sent them to SOC 68 to 22, which is where they were
    worked, not where they belong while open."""
    for summary in (
        "Henssler Financial - ThreatLocker Application Request for NAP-KIOSK",
        "launch.ps1 - ThreatLocker Application Request",
        "ThreatLocker Termination",
        "ThreatLocker Storage Request",
        "bxla90_11_52.exe - ThreatLocker Elevation Request",
    ):
        shape_routing = {summary_shape(summary): BOARD_SOC}
        decision = route_ticket(_ticket(1, summary), REGISTRY, ROUTING, shape_routing)
        assert decision.action == ACTION_LEAVE, summary


def test_maintenance_notice_moves_without_opening_an_outage():
    decision = route_ticket(_ticket(96175, "\U0001F535 Sentry is undergoing MAINTENANCE",
                                    contact="notifications"), REGISTRY, ROUTING)
    assert decision.action == ACTION_MOVE_AND_CLOSE


# --------------------------------------------------------------------------
# precision: a human ticket must never be swept up
# --------------------------------------------------------------------------

def test_a_human_ticket_is_left_alone():
    for ticket_id, summary, who in (
        (94016, "Outlook keeps signing me out every 30 seconds or so.", "Ike Dixon"),
        (95582, "Computer Mouse Pad and Keyboard Inoperative", "Justin Wagner"),
        (95928, "Trouble Accessing \\\\henssler.com\\shares", "Marsha Brooks"),
    ):
        decision = route_ticket(_ticket(ticket_id, summary, contact=who), REGISTRY, ROUTING)
        assert decision.action == ACTION_LEAVE, summary


def test_a_forwarded_vendor_email_with_no_contact_is_left_alone():
    """Live ticket 96013. Missing contact is NOT evidence of a machine: this
    is a marketing forward and moving it to NOC would hide a real message."""
    decision = route_ticket(
        _ticket(96013, "FW: Announcing Firm360 + Juno Partnership, Live Demo Webinar"),
        REGISTRY, ROUTING)
    assert decision.action == ACTION_LEAVE
    assert "no machine" in decision.reason.lower() or "human" in decision.reason.lower()


def test_a_machine_structure_with_no_contact_is_still_recognized():
    """Live tickets 96159 and 96165 have no contact but unmistakable
    structure. Structure decides when the contact cannot."""
    decision = route_ticket(
        _ticket(96159, "OP1460815: Microsoft 365 apps Service Health Incident (Service Restored)"),
        REGISTRY, ROUTING)
    assert decision.action == ACTION_MOVE_AND_TRACK
    assert decision.destination == BOARD_NOC


def test_every_decision_carries_a_reason():
    for ticket in (_ticket(1, "NOC Checks (AM/PM)", contact="Help Desk"),
                   _ticket(2, "my mouse is broken", contact="A Human"),
                   _ticket(3, "")):
        assert route_ticket(ticket, REGISTRY, ROUTING).reason


def test_tickets_already_off_triage_are_left_alone():
    """Penny only tidies the help desk board. A ticket a human deliberately
    filed to NOC is not hers to move again."""
    decision = route_ticket(
        _ticket(96178, "\U0001F7E0 Microsoft Azure is having a MINOR outage",
                contact="notifications", board=BOARD_NOC), REGISTRY, ROUTING)
    assert decision.action == ACTION_LEAVE


# --------------------------------------------------------------------------
# shape routing: what contact-only routing missed
# --------------------------------------------------------------------------

def test_device_names_collapse_to_one_shape():
    """424 tickets differ only by the hostname."""
    a = summary_shape("NAP-CONFROOM - Windows Workstation")
    b = summary_shape("GWH-MJ0HSEXZ - Windows Workstation")
    assert a == b


def test_shape_routing_is_learned_from_history():
    history = ([_ticket(i, f"HOST-{i:05d} - Windows Workstation", board=BOARD_NOC) for i in range(353)]
               + [_ticket(i, f"HOST-{i:05d} - Windows Workstation", board=BOARD_SOC) for i in range(66)]
               + [_ticket(i, f"HOST-{i:05d} - Windows Workstation", board="Triage") for i in range(5)])
    routing = learn_shape_routing(history)
    assert routing[summary_shape("NAP-CONFROOM - Windows Workstation")] == BOARD_NOC


def test_a_shape_humans_file_to_triage_is_not_learned():
    """The precision guard. A shape the help desk routinely receives is a
    human shape however many copies also ended up elsewhere."""
    history = ([_ticket(i, f"HOST-{i:05d} - printer jam", board="Triage") for i in range(40)]
               + [_ticket(i, f"HOST-{i:05d} - printer jam", board=BOARD_NOC) for i in range(60)])
    assert summary_shape("ANY-HOST - printer jam") not in learn_shape_routing(history)


def test_a_rare_shape_is_not_learned():
    history = [_ticket(i, f"HOST-{i} - odd one off", board=BOARD_NOC) for i in range(3)]
    assert learn_shape_routing(history) == {}


def test_a_contactless_device_alert_moves_on_shape_alone():
    """Live tickets 96181 and 96187. No contact, no outage wording, and
    contact-only routing left them on Triage."""
    shape_routing = {summary_shape("HOST-1 - Windows Workstation"): BOARD_NOC}
    decision = route_ticket(_ticket(96181, "NAP-CONFROOM - Windows Workstation"),
                            REGISTRY, ROUTING, shape_routing)
    assert decision.action == ACTION_MOVE_AND_CLOSE
    assert decision.destination == BOARD_NOC


def test_a_learned_shape_with_no_override_still_moves():
    """Shape routing must keep working for the shapes nobody has vetoed -
    the device alerts are the reason it exists."""
    shape = summary_shape("HOST-9 - Meeting Rooms")
    decision = route_ticket(_ticket(1, "NAP-CONF2 - Meeting Rooms"), REGISTRY, ROUTING,
                            {shape: BOARD_NOC})
    assert decision.action == ACTION_MOVE_AND_CLOSE
    assert decision.destination == BOARD_NOC


def test_never_move_shapes_beat_the_board_history():
    """Jerry, 2026-08-25: Email Notification tickets are not NOC tickets, even
    though 84 of 126 historically landed on NOC. A person who reads the
    contents outranks a board statistic."""
    shape_routing = {summary_shape("Email Notification"): BOARD_NOC}
    decision = route_ticket(_ticket(96194, "Email Notification", contact="notifications"),
                            REGISTRY, ROUTING, shape_routing)
    assert decision.action == ACTION_LEAVE
    assert "never-move" in decision.reason


def test_shape_routing_does_not_override_a_tracked_outage():
    shape = summary_shape("SP1461217: SharePoint Online Service Health Incident (Service Degradation)")
    decision = route_ticket(
        _ticket(96184, "SP1461217: SharePoint Online Service Health Incident (Service Degradation)"),
        REGISTRY, ROUTING, {shape: BOARD_SOC})
    assert decision.action == ACTION_MOVE_AND_TRACK


def test_a_human_ticket_is_still_left_alone_with_shape_routing_active():
    shape_routing = {summary_shape("HOST-1 - Windows Workstation"): BOARD_NOC}
    decision = route_ticket(_ticket(95582, "Computer Mouse Pad and Keyboard Inoperative",
                                    contact="Justin Wagner"), REGISTRY, ROUTING, shape_routing)
    assert decision.action == ACTION_LEAVE
