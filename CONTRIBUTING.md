# Contributing

Thanks for taking the time.

## Running it

```bash
git clone https://github.com/JSNavas/clauxy.git
cd clauxy
python3 tests/run.py          # no pytest, no install step
```

There is nothing to build and nothing to install. If `python3 tests/run.py`
passes, the offline checks pass. The VPN tests fake DNS, routes, interfaces,
and sockets; they never disconnect your real VPN or contact Anthropic.

CI also executes `tests/test_panel_refresh.js` with Node.js against the script
emitted by the Python renderer. It covers polling, focused controls, connection
recovery, timeouts, paging, tab visibility, and preservation of an open dialog.
Node is a development test tool; running Clauxy still requires only Python and
the operating system's route tools.

To try it against a live account you need the
[Claude CLI](https://claude.com/claude-code) on your PATH, at least one
subscription, and an active VPN routing the upstream API through a supported
tunnel. Runtime VPN checks support macOS `utun` and Linux TUN/WireGuard;
Linux requires iproute2 and socket-to-device binding permission:

```bash
CLAUXY_HOME=/tmp/clauxy-dev ./clauxy add test-1
CLAUXY_HOME=/tmp/clauxy-dev ./clauxy start --port 4099
```

`CLAUXY_HOME` keeps experiments away from your real setup.

## The one hard rule: no dependencies

clauxy is Python standard library only, on purpose, with no virtualenv or
lockfile. Runtime route checks use the operating system's tools (`route` and
`ifconfig` on macOS, iproute2 on Linux). A patch that adds a Python dependency
will be turned down unless it removes more complexity than it adds.

## Style

- Comments explain **why**, not what. If a line looks odd, say what broke
  without it — most comments here exist because something failed in production.
- Names in English, full words over abbreviations.
- All product-facing text stays in English: panel labels, CLI output, errors,
  connection status, and displayed month names. Keep this consistent regardless
  of the language used to discuss a change. Preserve user-entered labels.
- New behaviour comes with a test. The suite is plain functions and `assert`;
  `tests/helpers.py` has the fixtures.

## Things to know before changing the hot path

- **Use the selected protected transport.** Use `network_urlopen` from
  `clauxy_network.py`. VPN mode delegates to interface-bound sockets; Proxy mode
  establishes HTTP CONNECT/SOCKS5 tunnels with destination TLS. Never add a direct
  retry or automatic failover to the other mode. Proxy activation must validate
  before persisting credentials. Never render saved credential values or forward
  proxy authorization to the API. Cached display state never authorizes a socket.
  External CLI launches use `require_network(external=True)` and remain blocked
  in Proxy mode. Keep DNS, location-label, and external-process limits documented.
- **Requests pass through untouched.** Only the `Authorization` header is
  swapped. The upstream answers differently to a client it does not recognise,
  so the caller's own headers and body must survive verbatim.
- **Switching accounts drops the prompt cache**, which is where the cost is.
  Anything that makes switching more eager needs to justify it.
- **Not every 429 is spent quota.** A client the API does not recognise gets a
  429 with an empty message. Treating that as exhaustion drains the whole pool
  — a single health check would do it. See `is_quota_429`.
- **Both windows count.** An account can have a free hour and an exhausted
  week. Checking only the 5-hour window sends work to a dead account.

## Reporting bugs

`tail -50 ~/.clauxy/clauxy.log` usually says why. Tokens start with `sk-ant-`
— redact them before pasting anything.
