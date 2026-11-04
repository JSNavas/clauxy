# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
