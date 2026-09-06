"""Notification formatting and dispatch safety."""

from types import SimpleNamespace

from gittrack import notify


def _cfg(**over):
    base = dict(hourly=True, daily_hour=-1, macos=False, slack_webhook="", ntfy="",
                webhook="", smtp_host="", smtp_port=587, smtp_user="",
                smtp_password_env="X", email_from="", email_to="")
    base.update(over)
    return SimpleNamespace(notify=SimpleNamespace(**base))


ITEMS = [{"full_name": "a/one", "reasons": ["small and surging", "2.0x its pace"],
          "url": "https://github.com/a/one"}]


def test_format_is_one_repo_per_line_with_reasons_and_link():
    text = notify.format_items(ITEMS, "2 new")
    assert text.splitlines()[0] == "2 new"
    assert "* a/one - small and surging; 2.0x its pace" in text
    assert "https://github.com/a/one" in text


def test_send_with_nothing_configured_sends_nothing_and_does_not_raise():
    out = notify.send(_cfg(), ITEMS, "h")
    assert out == {"macos": False, "slack": False, "ntfy": False,
                   "webhook": False, "email": False}


def test_send_with_no_items_is_a_no_op_even_when_configured():
    out = notify.send(_cfg(ntfy="https://ntfy.sh/x", slack_webhook="https://x"), [], "h")
    assert not any(out.values())


def test_channels_configured_lists_only_what_is_on():
    assert notify.channels_configured(_cfg()) == []
    assert notify.channels_configured(_cfg(ntfy="https://ntfy.sh/t")) == ["ntfy"]
    assert notify.channels_configured(_cfg(macos=True, smtp_host="h", email_to="me@x")) \
        == ["macos", "email"]


def test_email_is_skipped_without_host_from_and_to():
    assert notify.email(_cfg(smtp_host="h"), "s", "b") is False
    assert notify.email(_cfg(smtp_host="h", email_from="a@x"), "s", "b") is False


def test_a_failing_channel_reports_false_rather_than_raising(monkeypatch):
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(notify.httpx, "post", boom)
    assert notify.slack("https://hooks.example/x", "t") is False
    assert notify.ntfy("https://ntfy.sh/t", "t", "b") is False
    assert notify.webhook("https://example/x", {}) is False
