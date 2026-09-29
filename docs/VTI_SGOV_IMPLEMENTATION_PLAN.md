# VTI / SGOV implementation plan

Description: Implement the fixed weekly trend research and confirmation workflow.
Last Updated: 2026-09-30
Status: Implementation verified; forward paper observation pending elapsed time
Audience: Developers and strategy operator

## Overview

The user-provided specification is authoritative: 200 completed sessions of
dividend-adjusted VTI closes, 1% entry/exit hysteresis, initially defensive,
weekly NYSE final-session evaluation, earliest next-session execution, and
first-weekly-checkpoint monthly rebalancing at 5 percentage points of VTI drift.
Risk-on weights are 80/18/2 and defensive weights 0/98/2 (VTI/SGOV/cash).
Only explicitly allocated strategy cash and whole-share lots are managed.
No real orders are authorized by research or paper operation.

## Global constraints

- Use the existing customization branch; no release, version bump, or PR.
- No account data, credentials, personal paths, or research outputs in Git.
- Default research capital is $10,000 virtual funds, not a live allocation.
- Use actual SGOV history only; explicitly state that earlier crises are absent.
- Adjusted close drives signals; raw OHLC and separately credited dividends
  drive the ledger. Reject unsupported splits instead of silently mispricing.
- LumiBot's Pandas backtesting clock drives a dedicated settlement-aware
  portfolio model. Its native unconstrained cash equity is not the result.
- Model historical T+2 before May 28, 2024 and T+1 afterwards, conservatively
  excluding exchange and federal bank holidays. Live settlement requires
  independent current broker confirmation, never just an estimated date.
- Model execution costs and dividend payment delay explicitly; no claim that
  assumptions reproduce actual fills, payment dates, taxes, or regulatory fees.
- Live submission requires a fresh exact-account snapshot, explicit allocated
  lots, settled-cash evidence, and per-order expiring confirmation. Uncertain
  submissions block further sends until reconciled; no automatic retries.

## Task 1: Shared rules and segregated accounting

Interfaces: Rules, MarketCalendar, Ledger, Intent, plan_orders.

1. Write failing tests for hysteresis equality, warmup, holidays, monthly gates,
   next-session execution, settlement, whole shares, and segregated holdings.
2. Implement pure rules and ledger with cumulative fill reconciliation.
3. Run focused tests with dotenv disabled. Expected: all pass, no network.

## Task 2: Research replay and LumiBot integration

Interfaces: Task 1 rules/ledger; daily data frames with raw OHLC, adjusted close,
dividend and split columns; daily engine callback consumed by Strategy.

1. Test no same-close fill, ex-date entitlement, delayed settled proceeds,
   cost sensitivity, stale/missing bars, and repeatable replay.
2. Add Yahoo public data cache with provenance/hash and validation; add three
   portfolio modes, latency/cost scenarios, parameter sensitivity and metrics.
3. Run the replay through LumiBot PandasDataBacktesting and compare its exact
   session/event path with direct deterministic replay. Expected: parity.
4. Download history and execute real research; retain ignored CSV/JSON/Markdown
   artifacts and report measured results, including limitations.

## Task 3: Confirmed execution boundary and local paper workflow

Interfaces: Task 1 ledger and plans; strict Schwab read adapter; persisted
submission journal and explicit confirmation bound to immutable order details.

1. Test wrong account, stale/absent settled-cash evidence, duplicates, expired
   confirmation, partial fills, timeout ambiguity, and restart idempotency.
2. Implement adapter and confirmation workflow. No live submit in validation.
3. Add paper init/check commands, atomic state and locks, virtual lots only,
   synthetic buy/partial/settlement/exit replay and daily catch-up.
4. Run paper once and configure a calendar-aware recurring paper check with
   a four-week review and eight-week observation boundary. Expected: no live
   transactions and no claims of completed future observation.

## Task 4: Documentation, verification and review

1. Add operator instructions to docsrc, engineering notes, and changelog entry.
2. Run focused tests, real research, replay and secret/diff checks.
3. Obtain one fresh whole-change review, fix substantive findings with tests.
4. Commit verified code to the existing customization branch. Keep generated
   research and all account information private. Report actual completion and
   remaining real-time paper observation / live allocation prerequisites.
