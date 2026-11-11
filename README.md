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

To call it from anywhere, link it onto your PATH:

```bash
ln -s "$PWD/clauxy" ~/.local/bin/clauxy    # or /usr/local/bin
clauxy status
```

It is a symlink to the checkout, so `git pull` is the whole upgrade.

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
clauxy start
clauxy stop
clauxy restart                # pick up new code, service or not
```

### Running it as a service

```bash
clauxy service install      # start on login, restart on crash
clauxy service status
clauxy service uninstall
```

| Platform | What gets installed |
|---|---|
| macOS | a launchd agent in `~/Library/LaunchAgents/com.clauxy.proxy.plist`, with `RunAtLoad` and `KeepAlive` |
| Linux | a systemd user unit in `~/.config/systemd/user/clauxy.service`, with `Restart=always` |

Both start clauxy when you log in and bring it back if it dies — verified on
macOS with `kill -9`, back in under 10 seconds.

This matters more than it looks: whatever points at clauxy has no fallback. If
the proxy is down, those clients are talking to a dead port.

While the service owns the port, `clauxy stop` will not touch it and says so:
launchd or systemd would only start it again. To pick up new code, use
`clauxy restart`, which asks the service manager to do it; to take it down for
good, `clauxy service uninstall`.

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

Your client does not have to be Claude Code, but the upstream has to believe it
is: a subscription token is only served when the system prompt's first block is
exactly `You are Claude Code, Anthropic's official CLI for Claude.` — otherwise
it answers 429, which looks like spent quota and is not. clauxy adds that block
for you, first and alone in it, and leaves your own prompt intact in the blocks
behind it. Nothing to configure.

## The panel

**http://127.0.0.1:4000/_status** — and `/_status.json` for scripts.

Per account: the live 5-hour and weekly bars straight from Anthropic, when each
one comes back, spend, requests, a token breakdown, and buttons to pause or
reorder. Below, every request with the account that served it, and every
account switch with the reason it happened.

The switch table and the request log are **paged five and ten rows at a time**,
and each page is fetched when you ask for it — the page does not carry history
it is not showing.

**It updates itself every second.** The page re-fetches only the part that
changes, so the account marked `IN USE` is the one serving right now rather
than the one that was serving up to ten seconds ago. It pauses while the tab is
in the background, and will not redraw while your cursor is in it, so a button
cannot move out from under a click.

The switch table survives restarts. Rows with no `FROM` are restarts, not
switches: nothing was handed over, the proxy simply came up on that account.

### Spend is indicative

On a subscription you do not pay per token. The dollar figures are what the
same traffic would cost at public API prices — useful to compare accounts
against each other, and nothing you are billed. The quota bars are the real
limit.

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
clauxy restart                stop and start, service or not
clauxy service install        start on login, restart on crash
clauxy check                  try every account with the real Claude CLI

clauxy login <alias>          optional, see below
```

## Quota for idle accounts

Quota arrives free in the `anthropic-ratelimit-unified-*` headers of every
response — no extra request, no credential that expires. But an account that
has not served anything yet has no headers, so the panel says **"quota not
known yet"**. It never claims an unknown account has quota: one you believe is
fresh may be fully drained.

`clauxy add` offers to fix that on the spot, and you can always do it later:

```bash
clauxy login <alias>
```

It is a regular sign-in in that account's isolated config dir, used **only to
read quota**. Requests keep going out with the long-lived token.

The **Refresh quota** button and the background probe both go to
`/api/oauth/usage`, which has a rate limit of its own — separate from the quota
it reports. If you hit it, the panel says `too many quota checks - retrying in
…` and stops asking until it clears. That is the telemetry endpoint being busy,
not the account being drained: the bars already on screen stay valid, and the
proxy keeps serving normally throughout.

Steady state is one request per account per minute, spread across the minute
rather than fired together. Readings survive restarts, so restarting does not
re-ask for something measured seconds ago, and the identity call happens once
every six hours instead of on every pass. What actually trips the limit is a
burst — a dozen restarts in a row, or holding down **Refresh quota**.

### Reusing a sign-in you already have

On macOS the credential lives in the Keychain, keyed by the **path** of the
config dir — so copying the folder does not bring it along. clauxy looks up
both the literal path and its realpath, which means a symlink works:

```bash
ln -s ~/somewhere/else/acct-1 ~/.clauxy/sessions/acct-1
```

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
- **The CLI banner in `system` is not documented either.** Which system prompts
  a subscription token is served for is Anthropic's call, not an API contract.
  If every account starts answering 429 with a bare `"Error"`, the check moved
  — not your quota.
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
