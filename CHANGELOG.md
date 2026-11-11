# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Any client can use clauxy**, not just Claude Code. A subscription token is
  served only to the CLI, and the upstream tells by the system prompt: its
  first block has to be exactly `You are Claude Code, Anthropic's official CLI
  for Claude.` Anything else answers 429 `rate_limit_error` with a bare
  `"Error"` — which reads like spent quota and is not. Measured on eight
  requests differing only in `system`, on an account at 0% of its window:
  absent → 429 (with or without `anthropic-beta`, so headers are not what is
  checked); the sentence alone → 200; the sentence with text glued behind it →
  429; the sentence in the second block → 429; the sentence first with blocks
  behind it → 200. So clauxy now puts that block first on `POST /v1/messages`
  and leaves the caller's own prompt intact in the blocks behind it — a code
  reviewer keeps its review prompt and gets served.
- `clauxy restart` — stop and start in one go, whether the proxy is run by
  hand or by the service. `clauxy stop` refuses to touch a service-managed
  proxy, and rightly so (launchd or systemd would start it again), which left
  "pick up new code" meaning "know the service label by heart".
- Both panel tables are **paged by the server**: five switches, ten log rows
  (the log paged at 15 in the browser before, and the switch table not at
  all).
  The page used to be handed every row and hide most of them with CSS — once
  a second, now that it refreshes itself — so it grew with history and sent
  the same hidden rows over and over. Clicking the pager asks for that slice
  instead, and a page out of range is clamped rather than left blank.
- The panel **updates itself every second** instead of reloading whole every
  ten. It re-fetches only the part that changes, pauses in a background tab,
  and will not redraw while the cursor is inside it, so a button cannot move
  out from under a click.
- Sign-ins **renew themselves**. The session token expires in hours, the
  refresh token lasts weeks, and only the Claude CLI is allowed to trade one
  for the other — so clauxy asks it to: a throwaway prompt against that
  account's config dir, with `ANTHROPIC_*` scrubbed so it does not come back
  through the proxy. A drained account renews too; the limit message arrives
  after the renewal. At most one attempt per account every 10 minutes.

### Fixed

- **A restart resurrected the `allowed_warning` verdict.** The state file
  stores `quota` already interpreted, so a file written by the old build kept
  `locked_reason: "allowed_warning"` and put the account back on the bench
  every time the proxy came up — nothing re-reads it until fresh headers
  arrive, and a bench-sat account never gets any. The raw `limits` are stored
  too, so the quota is now derived again on load.
- **The proxy hopped between drained accounts on every request** — a hundred
  switches in minutes, none of which could answer. With nothing usable the
  fallback pick kept naming a different account and `switch_reason` fired
  "out of quota" each time. Leaving an account that cannot serve is only
  worth it when the next one can; otherwise it stays put.
- **The limit line in the panel showed the JSON envelope**, cut mid-word at
  `{"type":"error","error":{"type":"rate_limit_error","message":"This request
  would exceed yo`. The 429 body was stored and printed raw. A new
  `limit_message()` pulls `error.message` out of it — on save and on render,
  so readings already on disk come out clean too — escaped, and with room for
  a sentence rather than braces.
- **An account at 90-something per cent was treated as blocked.** Anthropic
  sends `unified-7d-status: allowed_warning` as a window fills up; clauxy read
  anything that was not exactly `allowed` as a lock, marked the account out of
  quota and dropped it from the rotation. An account with a free hour and 93%
  of its week left unused was skipped while the proxy ping-ponged between two
  accounts that really were dry. Only a status that refuses work counts as a
  lock now; the whole `allowed*` family can still serve.
- **When nobody was comfortable, the work went to the driest account.** With
  every account over its threshold, the fallback chose "whose 5-hour window
  resets soonest" — which is the account on cooldown. It answered 429, the
  switch bounced back, and an account that was merely over 80% but perfectly
  able to answer never got a turn. The fallback now prefers whoever can
  actually serve, and only then whoever revives soonest.
- The 429 line under **Back in** showed the raw body — `{"type":"error",
  "error":{"type":"rate_limit_error","message":"This request would exceed yo`
  — so the envelope ate the line and the sentence was cut off mid-word. The
  message is pulled out of the JSON now, both where it is stored and where it
  is drawn (old readings already on disk included), and escaped on the way in.
- The panel contradicted itself: the header counted an account as drained
  while its own card said `ready`, and **Next if drained** named the next
  account in the list even when it was out of quota. Both now read the same
  availability the rotation does.
- **Every panel button did nothing.** The form posted
  `action=pause|resume|up|down|probe`; the handler read `accion` with Spanish
  values, so each click fell through to an empty branch and redirected. The
  dispatch now lives on the pool as `apply_control`, with the accepted names
  in one place and a test that renders the panel and checks the two sides
  still agree.
- The panel's **SWITCHES column read 0** while the log showed real switches.
  Only a 429 bumped the counter, so leaving an account because its hour filled
  up or because another one's quota expired sooner — the common case — was
  invisible. Arriving at the first account on startup is still not counted:
  nothing was left.
- The **switch table was wiped by every restart**. `audit` was saved to
  `state.json` and `events` was not, so the table showed a lone "startup" row
  and nothing else. Entries now survive, and carry a timestamp: one from two
  days ago shows its date instead of passing for a bare clock time.
- A **startup row read like missing data** — no `FROM`, because nothing was
  handed over. It now says so, in grey, and a table with no real switches in
  it says that too.
- **Spend was printed raw**: `$5.3123`. Totals now read like money (`$5.31`),
  while single requests keep enough digits to stay apart, since rounding
  fractions of a cent to two decimals turns the log into a column of `$0.02`.
- The quota probe **asked twice as often as it needed to, and again on every
  restart**. `/api/oauth/profile` was fetched alongside `/api/oauth/usage` on
  every pass, for an identity that does not change — it is now read once and
  then at most every six hours. Startup restored the saved state *after*
  probing, so a reading taken seconds earlier was thrown away and asked for
  again; a handful of restarts in a row was enough to earn a 429. State loads
  first now, a reading younger than the probe interval is left alone, and the
  backoff itself survives a restart instead of being skipped by one. The
  background probes are also spread across the minute rather than fired
  together, since bursts are what the endpoint objects to.
- **`HTTP 429` next to the token counts** read as if the account were drained.
  It was `/api/oauth/usage`, the telemetry endpoint, which rate-limits
  separately from the quota it reports. The message now says what it is and
  how long is left, and — the actual fix — clauxy stops asking: the button and
  the once-a-minute probe both honour a backoff taken from `Retry-After`.
  Nothing before stopped it from earning the next 429.
- A limit message naming a date **that had just passed** was rolled a whole
  year forward, and the 8-day cap then parked the account for the better part
  of a week. The year is now whichever one puts the date closest to now, which
  is also what makes `Jan 2` read as next year on December 31. The test that
  should have caught it hard-coded `Nov 5`, so it only failed on the days it
  felt like.
- Quota **froze at its last reading**. With the sign-in expired there was no
  telemetry, and the panel kept showing a percentage from a window that had
  already reset — including accounts stuck at 100% whose limit had lifted.
  An expired reading now reads "unknown since it reset" instead of 0%, which
  claimed an empty window nobody had measured.
- `probe_quota` reported the result of the **last** call it made, so a
  `/profile` that would not answer printed "quota unknown" over quota it had
  just read correctly. Only the usage call decides that message now, and an
  expired sign-in says so instead of hiding behind it.
- Reset times **wobbled by a minute** between page refreshes, and showed the
  wrong hour: they were rebuilt from a whole-second countdown, and the resets
  land on 12:59:59. Taken from the reset itself and rounded to the minute.
- `clauxy status` printed "no quota data" when only one of the two windows
  had aged out, hiding the one that was still good. Each window stands alone.

- The service looked for the proxy next to the **symlink** instead of the
  checkout, so installing it from a `PATH` link started a process that died in
  a loop. `realpath`, not `abspath`.
- The panel claimed **"Quota available"** for accounts it had no telemetry
  for. Not knowing is not the same as having quota — that is how work gets
  sent to a drained subscription. They now read "Quota not known yet" and the
  header counts them separately.
- `/favicon.ico` was forwarded upstream and earned a 404 per page load, which
  filled the request log. Served as 204 now.

### Changed

- **The weekly window is now spent to 100%**, not 98%. The hour refills; the
  week does not, so every percent left when it rolls over is thrown away. The
  5-hour threshold keeps its margin at 80% for exactly that reason — leaving a
  little in an hour costs nothing.
- Quota bars show the **wall-clock reset time** in the viewer's own timezone
  ("resets at 9:59 PM") instead of repeating the countdown already in the
  headline.
- A bare `clauxy` lists what it can do, grouped, instead of an argparse error.

## [0.1.0] - 2026-11-04

First release.

### Added

- Pass-through proxy over the Anthropic Messages API: the request reaches the
  upstream untouched except for the `Authorization` header.
- Transparent failover — on a quota 429 the **same request** is retried on the
  next account, so the client never sees it.
- Account selection by perishability: among usable accounts, the one whose
  weekly window resets soonest, because that quota expires either way.
- Switching thresholds (80% of the 5-hour window, 98% weekly) so a switch
  happens before the wall, with a cooldown and margin that stop two accounts
  from ping-ponging and discarding the prompt cache.
- Live quota from the `anthropic-ratelimit-unified-*` response headers, with
  `clauxy login` as an optional source for accounts that have no traffic yet.
- Web panel: per-account quota bars, reset countdowns, spend, token breakdown,
  request log, switch log with reasons, and pause/reorder controls.
- `clauxy service install` — launchd on macOS, systemd on Linux — to start on
  login and restart on crash.
- State persisted to `~/.clauxy/state.json`, so counters and holds survive a
  restart.
- Tokens in files (`chmod 600`) by default, macOS Keychain optional, and
  environment variables for containers.
