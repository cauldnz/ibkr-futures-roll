# Changelog

## 0.1.0 - 2026-11-20
- First release: reads futures positions from TWS, rolls the nearest expiry of each product as one passive calendar-spread order (BUY next / SELL current), dry run by default.
- Spread price steps for ZN (1/128) and ZT (1/256) confirmed against TWS; UB and CME micro equity-index steps inferred from book quotes.
- Agent-friendly CLI: `--list`, `--status`, `--json`, exit codes, PEP 723 header for `uv run`.
- Optional TOML config: pairs, per-product window, target month, spread step, skip.
- Security hardening before release: crossing prices are refused (never staged, even with `--live --yes`); a failed TWS what-if check means not staged; `--ticks-away` must be 0 or more; `--limit` input validated and warned when unverifiable; `--yes` only with `--live`; the confirmation prompt goes to stderr so `--json` stdout stays clean; the loaded config path is printed; `ib_async` pinned below 3; CI runs with read-only permissions.
