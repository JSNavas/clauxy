"""Token counting and cost, including the compressed-body trap."""

import gzip
import json

from helpers import load

proxy = load()

SSE = (b'event: message_start\n'
       b'data: {"type":"message_start","message":{"model":"claude-sonnet-5-5",'
       b'"usage":{"input_tokens":1500,"cache_creation_input_tokens":200,'
       b'"cache_read_input_tokens":30000,"output_tokens":1}}}\n\n'
       b'event: message_delta\n'
       b'data: {"type":"message_delta","usage":{"output_tokens":450}}\n\n')

PLAIN = json.dumps({
    "model": "claude-haiku-5-5",
    "usage": {"input_tokens": 800, "output_tokens": 120,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5000},
}).encode()


def test_counts_a_gzipped_sse_stream():
    """Clients ask for gzip; reading it as text reported 0 tokens on everything."""
    model, usage = proxy.usage_from_body(
        proxy.decompress(gzip.compress(SSE), "gzip"))
    assert model == "claude-sonnet-5-5"
    assert usage["input"] == 1500
    assert usage["output"] == 450          # from message_delta, not message_start
    assert usage["cache_read"] == 30000


def test_counts_a_plain_sse_stream():
    _, usage = proxy.usage_from_body(proxy.decompress(SSE, None))
    assert usage["output"] == 450


def test_counts_a_non_streaming_json_response():
    model, usage = proxy.usage_from_body(
        proxy.decompress(gzip.compress(PLAIN), "gzip"))
    assert model == "claude-haiku-5-5"
    assert usage["input"] == 800
    assert usage["cache_read"] == 5000


def test_a_truncated_body_does_not_break_the_request():
    """Only the first 256 KB are kept, so inflation can fail. Counting is
    optional; serving the client is not."""
    assert proxy.decompress(gzip.compress(SSE)[:40], "gzip") == b""


def test_unknown_encoding_passes_through():
    assert proxy.decompress(b"xyz", "br") == b"xyz"


def test_cost_uses_cache_pricing():
    """Cache writes bill at 1.25x input, reads at the reduced rate."""
    usage = {"input": 1500, "output": 450, "cache_write": 200, "cache_read": 30000}
    cost = proxy.cost_usd("claude-sonnet-5-5", usage)
    expected = (1500 * 2.0 + 450 * 10.0 + 200 * 2.0 * 1.25 + 30000 * 0.2) / 1e6
    assert abs(cost - expected) < 1e-9
