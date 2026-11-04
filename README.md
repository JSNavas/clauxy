# clauxy

[![CI](https://github.com/JSNavas/clauxy/actions/workflows/ci.yml/badge.svg)](https://github.com/JSNavas/clauxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![No dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#)

**One endpoint. Many Claude subscriptions. No interruptions.**

Point any Claude-compatible tool at `http://127.0.0.1:4000` and clauxy serves
every request from whichever of your accounts has quota. When one runs out, the
**same request** is retried on the next one — the client never sees the 429.

Zero dependencies. One Python file and a CLI, standard library only.

```
   your tools (Claude Code, an IDE, an agent framework, a script)
                              │
                 ANTHROPIC_BASE_URL=http://127.0.0.1:4000
                              │
                           clauxy
                              │
            acct-1  ──►  acct-2  ──►  acct-3  ──►  ...
          spend the one whose quota expires first
```

## Why

A Claude subscription has two independent limits: a rolling 5-hour window and a
weekly one. Hit either and your work stops until it resets. If you pay for more
than one subscription, nothing joins them up — you switch by hand and lose your
place.

clauxy pools them behind one URL and picks for you.

## Install

Requires Python 3.10+ and the [Claude CLI](https://claude.com/claude-code) on
your PATH.

```bash
git clone https://github.com/JSNavas/clauxy.git
cd clauxy
./clauxy add acct-1      # repeat per subscription
```

`add` opens a browser sign-in and captures the long-lived token from
`claude setup-token`. Use a **private window** between accounts, or you will
register the same one twice.

Then run it:

```bash
./clauxy service install      # start on login, restart on crash
./clauxy status
```

Or by hand, for a try-out:

```bash
./clauxy start
```

## Point your tools at it

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
```

That is the whole integration. Anything that speaks the Anthropic Messages API
and honours `ANTHROPIC_BASE_URL` works — the Claude CLI, IDE extensions, agent
frameworks, your own scripts. Tools with their own connection settings (a
"custom endpoint" or "local endpoint" field) take the same URL.

The bearer token your client sends is ignored: clauxy substitutes the account's
own. Any placeholder will do.

## The panel

**http://127.0.0.1:4000/_status** — and `/_status.json` for scripts.

Per account: the live 5-hour and weekly bars straight from Anthropic, when each
one comes back, spend, requests, a token breakdown, and buttons to pause or
reorder. Below, every request with the account that served it, and every
account switch with the reason it happened.

## How it picks

Among accounts that still have room, clauxy serves the one whose **weekly
window resets soonest**. That quota is perishable — whatever you do not spend
before the reset is gone — so the one about to expire is spent first.

| Threshold | Default | Why |
|---|---|---|
| 5-hour window | 80% | move before the wall, instead of spending a 429 to find it |
| Weekly window | 98% | same, on the limit that actually runs scarce |

**It does not switch on every request**, and that is deliberate: each switch
throws away the prompt cache, which is where the real cost is. A 5-minute
cooldown and a 5-point margin keep two accounts near the line from
ping-ponging. Emergencies skip the cooldown.

`--mode sticky` instead keeps each client on the account its own token maps to,
borrowing another only when that one runs out.

## Commands

```
clauxy add <alias>            register an account
clauxy list                   accounts and labels
clauxy label <alias> <text>   name an account in the panel
clauxy remove <alias>         forget an account

clauxy start | stop | status  run the proxy by hand
clauxy service install        start on login, restart on crash
clauxy check                  try every account with the real Claude CLI

clauxy login <alias>          optional, see below
```

## Quota for idle accounts

Quota arrives free in the `anthropic-ratelimit-unified-*` headers of every
response — no extra request, no credential that expires. But an account that
has not served anything yet has no headers, so it shows as unknown. That is a
trap: an account you think is fresh may be exhausted.

`clauxy login <alias>` adds a regular sign-in, in an isolated config dir, that
lets clauxy query quota directly. Requests keep using the long-lived token; the
session credential is only ever read.

## Configuration

Everything lives in `~/.clauxy` (override with `CLAUXY_HOME`):

```
~/.clauxy/
  tokens/<alias>.token    account tokens, chmod 600
  accounts.json           alias -> label
  state.json              counters, holds and request log
  sessions/<alias>/       isolated config dir per account
  clauxy.log
```

For containers and CI, env vars replace the files: `CLAUXY_ACCOUNTS=a,b,c` and
`CLAUXY_TOKEN_A=...`. On macOS, `clauxy add --keychain` stores tokens in the
Keychain instead of files.

## Security

**Do not expose the port.** It binds `127.0.0.1` on purpose: anything that
reaches it spends your subscriptions with no credential at all. Behind a tunnel
or on `0.0.0.0`, you are handing them out.

Tokens are stored `chmod 600` and never leave the machine.

## Caveats, honestly

- **The quota headers are not officially documented.** They work today and may
  change without notice. If the bars go blank, that is the first thing to check.
- **Switching accounts drops the prompt cache.** On cache-heavy workloads a
  switch costs real money on the new account. clauxy minimises switches for
  exactly this reason, but it cannot avoid them.
- **Pooling consumer subscriptions is a grey area.** Check Anthropic's current
  terms before relying on this. Other projects in this space say the same.
- **Mid-stream failures are not rescued.** Once a response starts streaming,
  clauxy cannot switch without losing it. Your client's own retry covers that.

## Prior art

[claude-rotate](https://github.com/doxaras/claude-rotate) does the same job and
got there first; clauxy borrows its perishable-quota rule, its thresholds and
its 429 classification. It has features clauxy does not (multi-device keys,
per-device analytics, an OpenAI-compatible gateway) at the cost of a FastAPI
stack. clauxy trades those for zero dependencies and knowing the state of every
account from the moment it starts.

## Contributing

Bug reports and patches are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
The whole test suite is `python3 tests/run.py`: no pytest, no install step.

The one rule is **no dependencies**. clauxy is standard library only so it runs
wherever Python does, with nothing to audit.

## License

[MIT](LICENSE)
