"""The CLI banner in `system`: what the upstream accepts, and what it 429s."""

from helpers import load

proxy = load()
BANNER = proxy.CLI_SYSTEM


def first(payload):
    return payload["system"][0]


def test_adds_the_banner_when_there_is_no_system():
    out = proxy.with_cli_system({"model": "m", "messages": []})
    assert out["system"] == [{"type": "text", "text": BANNER}]


def test_keeps_the_callers_prompt_behind_the_banner():
    """What lets a reviewer keep its own prompt: the banner only goes first."""
    out = proxy.with_cli_system({"system": "Review this diff."})
    assert first(out) == {"type": "text", "text": BANNER}
    assert out["system"][1] == {"type": "text", "text": "Review this diff."}


def test_leaves_an_exact_string_system_alone():
    payload = {"system": BANNER}
    assert proxy.with_cli_system(payload)["system"] == BANNER


def test_splits_text_glued_behind_the_banner():
    """The banner plus text in ONE block is rejected; two blocks are not."""
    out = proxy.with_cli_system({"system": BANNER + "\n\nAlso do X."})
    assert first(out) == {"type": "text", "text": BANNER}
    assert out["system"][1]["text"] == "Also do X."


def test_leaves_an_array_that_already_starts_with_the_banner():
    system = [{"type": "text", "text": BANNER},
              {"type": "text", "text": "mine"}]
    out = proxy.with_cli_system({"system": list(system)})
    assert out["system"] == system


def test_prepends_when_the_banner_is_not_the_first_block():
    out = proxy.with_cli_system({"system": [
        {"type": "text", "text": "mine"},
        {"type": "text", "text": BANNER},
    ]})
    assert first(out) == {"type": "text", "text": BANNER}
    assert len(out["system"]) == 3


def test_keeps_cache_control_on_the_callers_blocks():
    out = proxy.with_cli_system({"system": [
        {"type": "text", "text": "mine",
         "cache_control": {"type": "ephemeral"}},
    ]})
    assert out["system"][1]["cache_control"] == {"type": "ephemeral"}
