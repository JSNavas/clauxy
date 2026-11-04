"""The panel adapts to however many accounts are loaded."""

import re

from helpers import account, load, pool

proxy = load()


def _cards(html):
    return len(re.findall(r'<article class="card', html))


def _header_count(html):
    m = re.search(r"Accounts</span>\s*<b>(\d+)</b>\s*<i class=hint>([^<]+)", html)
    return (int(m.group(1)), m.group(2)) if m else (None, None)


def test_renders_any_number_of_accounts():
    for n in (1, 2, 3, 5, 8):
        html = proxy.render_panel(
            pool(*[account(f"a{i}") for i in range(n)]).snapshot())
        assert _cards(html) == n
        assert _header_count(html)[0] == n


def test_header_summarises_drained_accounts():
    html = proxy.render_panel(pool(
        account("ok1"), account("ok2"),
        account("gone1", util_7d=100), account("gone2", util_7d=100)).snapshot())
    _, summary = _header_count(html)
    assert "2 with quota" in summary and "2 drained" in summary


def test_names_the_account_that_will_serve_before_any_traffic():
    html = proxy.render_panel(pool(account("first"), account("second")).snapshot())
    assert "no requests yet" in html
    assert "IN USE" in html


def test_a_drained_week_is_called_out():
    html = proxy.render_panel(pool(account("a", util_7d=100)).snapshot())
    assert "WEEKLY LIMIT REACHED" in html


def test_an_unused_window_says_so_instead_of_dashes():
    """No usage means no reset time; 'resets in --' looks like missing data."""
    assert "unused" in proxy.reset_label({"pct": 0, "in_s": None})
    assert proxy.reset_label({"pct": 40, "in_s": None}) == ""


def test_never_claims_quota_it_does_not_know_about():
    """An account with no telemetry is not an account with quota.

    Saying 'Quota available' for one we have never heard from is how work
    gets sent to a drained subscription.
    """
    blank = account("blank")
    blank.quota = {}
    html = proxy.render_panel(pool(blank).snapshot())
    assert "Quota available" not in html
    assert "Quota not known yet" in html
    assert "QUOTA UNKNOWN" in html


def test_header_counts_unknown_apart_from_with_quota():
    good = account("good", util_5h=10, util_7d=10)
    blank = account("blank")
    blank.quota = {}
    html = proxy.render_panel(pool(good, blank).snapshot())
    _, summary = _header_count(html)
    assert "1 with quota" in summary and "1 unknown" in summary
