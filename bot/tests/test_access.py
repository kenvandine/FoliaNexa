from __future__ import annotations

from folia_bot.access import decide_auto_approve


def test_no_configured_role_never_auto_approves():
    assert decide_auto_approve({111, 222}, None) is False


def test_member_with_configured_role_auto_approves():
    assert decide_auto_approve({111, 222}, 222) is True


def test_member_without_configured_role_does_not_auto_approve():
    assert decide_auto_approve({111, 222}, 999) is False


def test_no_roles_does_not_auto_approve():
    assert decide_auto_approve(set(), 222) is False
