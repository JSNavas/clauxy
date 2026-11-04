# Contributing

Thanks for taking the time.

## Running it

```bash
git clone https://github.com/JSNavas/clauxy.git
cd clauxy
python3 tests/run.py          # no pytest, no install step
```

There is nothing to build and nothing to install. If `python3 tests/run.py`
passes, you have a working checkout.

To try it against a live account you need the
[Claude CLI](https://claude.com/claude-code) on your PATH and at least one
subscription:

```bash
CLAUXY_HOME=/tmp/clauxy-dev ./clauxy add test-1
CLAUXY_HOME=/tmp/clauxy-dev ./clauxy start --port 4099
```

`CLAUXY_HOME` keeps experiments away from your real setup.

## The one hard rule: no dependencies

clauxy is standard library only, on purpose. It runs where Python runs, with no
virtualenv, no lockfile and nothing to audit. A patch that adds a dependency
will be turned down unless it removes more complexity than it adds.

## Style

- Comments explain **why**, not what. If a line looks odd, say what broke
  without it — most comments here exist because something failed in production.
- Names in English, full words over abbreviations.
- New behaviour comes with a test. The suite is plain functions and `assert`;
  `tests/helpers.py` has the fixtures.

## Things to know before changing the hot path

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
