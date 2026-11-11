"""Account selection: who serves next, and when to switch."""

import time

from helpers import account, pool


def test_spends_the_perishable_account_first():
    """Weekly quota is use-it-or-lose-it, so burn the one expiring soonest."""
    far = account("far", util_5h=10, util_7d=20, reset_5h_h=4, reset_7d_h=96)
    soon = account("soon", util_5h=10, util_7d=20, reset_5h_h=2, reset_7d_h=2)
    p = pool(far, soon)
    assert p.pick().alias == "soon"
    assert [a.alias for a in p.order()][0] == "soon"


def test_does_not_switch_on_every_request():
    """Each switch throws away the prompt cache, so stickiness is the point."""
    p = pool(account("a", util_7d=50, reset_7d_h=50),
             account("b", util_7d=50, reset_7d_h=51))
    first = [x.alias for x in p.order()][0]
    assert all([x.alias for x in p.order()][0] == first for _ in range(20))


def test_switches_before_hitting_the_wall():
    """At 80% of the 5-hour window, move rather than spend a 429 finding out."""
    p = pool(account("hot", util_5h=85), account("cool", util_5h=10))
    p.active = "hot"
    assert not p.find("hot").usable
    assert [x.alias for x in p.order()][0] == "cool"
    assert "80%" in p.events[0]["reason"]


def test_cooldown_stops_two_accounts_ping_ponging():
    p = pool(account("a", util_5h=82), account("b", util_5h=80.5))
    p.active = "a"
    p.last_switch = time.time()
    before = len(p.events)
    for _ in range(10):
        p.order()
    assert len(p.events) - before <= 1


def test_exhausted_weekly_skips_the_cooldown():
    """Only a FULL week forces the jump now; 99% is still quota to spend."""
    p = pool(account("full", util_7d=100), account("free", util_7d=20))
    p.active = "full"
    p.last_switch = time.time()
    assert [x.alias for x in p.order()][0] == "free"
    assert "quota" in p.events[0]["reason"]


def test_weekly_exhaustion_counts_even_with_a_free_hour():
    """Two independent limits: a free hour does not make a drained week usable."""
    drained = account("drained", util_5h=0, util_7d=100)
    assert drained.out_of_quota
    assert not drained.usable


def test_falls_back_to_whoever_revives_soonest():
    p = pool(account("late", util_5h=100, reset_5h_h=5),
             account("early", util_5h=100, reset_5h_h=1))
    assert p.pick().alias == "early"


def test_a_planned_switch_is_counted():
    """The panel's SWITCHES column read 0 while the log showed real switches.

    Only a 429 used to bump the counter, so leaving an account because its
    hour filled up — the common case — was invisible.
    """
    p = pool(account("hot", util_5h=85), account("cool", util_5h=10))
    p.active = "hot"
    p.order()
    assert p.find("hot").switches_away == 1
    assert p.failovers == 1
    assert p.find("cool").switches_away == 0


def test_startup_is_not_a_switch():
    """Arriving at the first account is not leaving anything."""
    p = pool(account("a"), account("b"))
    p.order()
    assert p.failovers == 0
    assert all(a.switches_away == 0 for a in p.accounts)


def test_never_sends_work_to_a_drained_account_over_a_live_one():
    """When nobody is comfortable, serve with whoever can actually answer.

    Picking "whose hour resets soonest" sent every request to the account on
    cooldown - which answers 429 and bounces back - while an account that was
    merely over the threshold sat there, able to serve.
    """
    dry = account("dry", util_5h=100, reset_5h_h=1)
    dry.cool_down(2700)
    hot = account("hot", util_5h=92, reset_5h_h=3, reset_7d_h=40)
    p = pool(dry, hot)
    p.active = "dry"
    assert not dry.available and hot.available
    assert not hot.usable          # over 80%, but alive
    assert p.pick().alias == "hot"
    assert [a.alias for a in p.order()][0] == "hot"


def test_the_last_percent_of_the_week_is_not_handed_back():
    """Weekly quota does not refill: whatever is left when it rolls over is lost."""
    a = account("nearly", util_5h=10, util_7d=99)
    assert a.available and a.usable, "99% weekly must still take work"
    assert not account("done", util_7d=100).usable


def test_an_account_with_1_percent_left_still_gets_the_work():
    """One that can serve beats three that cannot, however little it has left."""
    dry_a = account("dry_a", util_5h=100, util_7d=10)
    alive = account("alive", util_5h=99, util_7d=99)
    dry_b = account("dry_b", util_5h=100, util_7d=20)
    p = pool(dry_a, alive, dry_b)
    assert p.pick() is alive


def test_all_dry_means_stay_put_instead_of_hopping():
    """Switching between drained accounts answers nothing and spams the log.

    With every account out of quota the fallback pick kept naming a different
    one, and `switch_reason` fired "out of quota" on every request: a hundred
    switches in minutes, none of which could serve.
    """
    a, b = account("a", util_5h=100), account("b", util_5h=100)
    p = pool(a, b)
    p.active = "a"
    assert not a.available and not b.available
    assert p.switch_reason(a, b) is None, "nothing to gain from the hop"
