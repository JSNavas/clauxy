"""The panel adapts to however many accounts are loaded."""

import os
import re
import time

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


def test_the_buttons_ask_for_something_the_pool_understands():
    """Every button was dead: the form posted English, the handler read Spanish.

    Pinning the rendered values against the accepted ones is what stops the
    two sides drifting apart again without a word.
    """
    p = pool(account("a"), account("b"))
    html = proxy.render_panel(p.snapshot())
    asked = set(re.findall(r'name="action" value="(\w+)"', html))
    assert asked, "no buttons rendered"
    assert asked <= set(proxy.Pool.CONTROLS), asked - set(proxy.Pool.CONTROLS)


def test_pausing_from_the_panel_pauses():
    p = pool(account("a"), account("b"))
    assert p.apply_control("a", "pause")
    assert p.find("a").paused
    assert p.apply_control("a", "resume")
    assert not p.find("a").paused


def test_an_unknown_button_changes_nothing():
    p = pool(account("a"))
    assert not p.apply_control("a", "rm -rf")
    assert not p.apply_control("nobody", "pause")
    assert not p.find("a").paused


def test_the_panel_refreshes_itself_without_a_reload():
    """A 10 s meta-refresh could show IN USE on an account already left."""
    html = proxy.render_panel(pool(account("a")).snapshot())
    assert "http-equiv=refresh" not in html
    assert 'id=live' in html and "setInterval(tick, 1000)" in html


def test_spend_reads_like_money():
    """"$5.3123" is not a dollar amount anyone writes."""
    assert proxy.money(5.3123) == "$5.31"
    assert proxy.money(1234.5) == "$1,234.50"
    assert proxy.money(0) == "$0.00"
    assert proxy.money(None) == "—"
    # A single request costs fractions of a cent: rounding those to two
    # decimals would turn the whole log into a column of "$0.02".
    assert proxy.money(0.01828) == "$0.018"
    assert proxy.money(0.00031) == "$0.00031"


def test_switch_history_survives_a_restart(tmp=None):
    """The table used to show a lone "startup" row after every restart."""
    import json
    import tempfile
    p = pool(account("a"), account("b"))
    p.record_switch(p.find("a"), p.find("b"), "5h >= 80%")
    with tempfile.TemporaryDirectory() as d:
        old = proxy.STATE_FILE
        proxy.STATE_FILE = os.path.join(d, "state.json")
        try:
            proxy.save_state(p)
            assert json.load(open(proxy.STATE_FILE))["events"]
            fresh = pool(account("a"), account("b"))
            proxy.load_state(fresh)
        finally:
            proxy.STATE_FILE = old
    assert [e["reason"] for e in fresh.events] == ["5h >= 80%"]
    assert fresh.events[0]["from"] == "a" and fresh.events[0]["to"] == "b"


def test_an_old_switch_is_dated_not_just_clocked():
    now = proxy.switch_when({"ts": time.time(), "time": "x"})
    then = proxy.switch_when({"ts": time.time() - 3 * 86400, "time": "x"})
    assert re.fullmatch(r"\d\d:\d\d:\d\d", now), now
    assert re.match(r"[A-Z][a-z]{2} \d+ \d\d:\d\d:\d\d", then), then
    assert proxy.switch_when({"time": "16:14:50"}) == "16:14:50"


def test_a_restart_row_does_not_read_as_a_broken_switch():
    """An empty FROM looked like missing data. Nothing was left: it booted."""
    p = pool(account("a"), account("b"))
    p.order()
    html = proxy.render_panel(p.snapshot())
    assert "No switches yet" in html
    assert "proxy started" in html
    assert "<td>—</td>" not in html


def test_a_real_switch_still_names_both_sides():
    p = pool(account("hot", util_5h=85), account("cool", util_5h=10))
    p.active = "hot"
    p.order()
    html = proxy.render_panel(p.snapshot())
    assert "No switches yet" not in html
    assert "<td>hot</td>" in html and "<b>cool</b>" in html
