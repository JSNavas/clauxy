# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Sign-ins **renew themselves**. The session token expires in hours, the
  refresh token lasts weeks, and only the Claude CLI is allowed to trade one
  for the other — so clauxy asks it to: a throwaway prompt against that
  account's config dir, with `ANTHROPIC_*` scrubbed so it does not come back
  through the proxy. A drained account renews too; the limit message arrives
  after the renewal. At most one attempt per account every 10 minutes.

### Fixed

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

- Quota bars show the **wall-clock reset time** in the viewer's own timezone
  ("resets at 9:59 PM") instead of repeating the countdown already in the
  headline.
- The request log pages at 15 rows.
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
