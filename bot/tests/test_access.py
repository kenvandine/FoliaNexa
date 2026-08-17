from __future__ import annotations

from types import SimpleNamespace

from folia_bot.access import compute_role_sync_ids, decide_auto_approve, role_membership_changed


def test_no_configured_role_never_auto_approves():
    assert decide_auto_approve({111, 222}, None) is False


def test_member_with_configured_role_auto_approves():
    assert decide_auto_approve({111, 222}, 222) is True


def test_member_without_configured_role_does_not_auto_approve():
    assert decide_auto_approve({111, 222}, 999) is False


def test_no_roles_does_not_auto_approve():
    assert decide_auto_approve(set(), 222) is False


def test_role_membership_changed_true_on_gain():
    assert role_membership_changed({111}, {111, 222}, 222) is True


def test_role_membership_changed_true_on_loss():
    assert role_membership_changed({111, 222}, {111}, 222) is True


def test_role_membership_changed_false_when_unrelated_role_changes():
    assert role_membership_changed({111}, {111, 333}, 222) is False


def test_role_membership_changed_false_when_nothing_changes():
    assert role_membership_changed({111, 222}, {111, 222}, 222) is False


def test_compute_role_sync_ids_returns_empty_for_unconfigured_role():
    assert compute_role_sync_ids(None) == []


def test_compute_role_sync_ids_returns_member_ids():
    fake_role = SimpleNamespace(members=[SimpleNamespace(id=111), SimpleNamespace(id=222)])
    assert compute_role_sync_ids(fake_role) == ["111", "222"]


def test_compute_role_sync_ids_returns_empty_list_for_role_with_no_members():
    fake_role = SimpleNamespace(members=[])
    assert compute_role_sync_ids(fake_role) == []
