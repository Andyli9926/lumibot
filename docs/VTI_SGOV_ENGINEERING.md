# VTI / SGOV engineering notes

Description: Accounting and execution boundaries for the weekly ETF strategy.
Last Updated: 2026-09-30
Status: Research and forward paper observation; live use requires operator evidence
Audience: Developers and strategy operator

## Overview

The user guide is `docsrc/VTI_SGOV_WORKFLOW.rst`. Research, paper simulation and
live execution are separate entry points. Neither the research nor paper CLI can
submit real orders. All generated artifacts belong under ignored `.lumibot/`.
The default capital is a virtual $10,000, not an allocation from an account.

## Chronology and accounting

`Rules` uses only the latest 200 completed adjusted closes; thresholds are
inclusive. NYSE's final session each ISO week is the checkpoint, including
holiday Thursdays and shortened Fridays. A monthly checkpoint is the first
such session in a calendar month. State changes override the 5-point VTI drift
gate; other weekly checkpoints cannot rebalance.

Each Engine session settles modeled receivables, records dividend entitlement
on opening lots, executes previously queued decisions at the raw open, values
at raw close, and only then generates the next signal. A state change queues
the next session plus the configured human-confirmation delay. Cash initially
buys the defensive portfolio; no same-day weekly signal influences that trade.
Deferred buys complete only the current switch/rebalance batch. Dividend cash
alone cannot keep an ordinary rebalance batch running indefinitely.

LumiBot `PandasDataBacktesting` drives all three main portfolio runs through
`VtiSgovResearch`. The native broker has no positions: the reported equity and
fills come from the explicit settlement-aware Engine ledger, not native broker
statistics. This avoids claiming native market fills enforce cash settlement.
The daily pandas clock advances strictly past its initial timestamp; it is
seeded the day before the first data row. A parity test checks every session and
trade against direct replay. Sensitivity runs use that same verified Engine.

Raw prices plus separately credited dividends drive returns. Adjusted closes
only drive trend signals. Ex-date purchases do not receive that day's dividend.
Dividend receivables enter equity immediately but are spendable only after a
modeled five-session payment delay. Yahoo does not supply reliable payment
dates here; this is explicitly an assumption. Nonzero splits or capital-gains
distributions are unsupported and stop validation. Missing bars are not filled.

Sale settlement uses T+2 before 2024-05-28 and T+1 afterwards. The research
calendar conservatively excludes NYSE and federal bank holidays; exact clearing
calendars and extraordinary closures can differ. Live release needs specific
broker settlement evidence as well as sufficient fresh settled cash.

Costs assume 5 bps per side for spread plus slippage and a $0.10 allowance per
sell; the second scenario doubles both. The allowance is NOT historical actual
SEC/FINRA fees. Online ETF commission is modeled as zero. Taxes and idle-cash
interest are excluded; ETF expenses are already embedded in prices. The opening
fill assumption is not proof that a real DAY limit order would fill.

## Live boundary

`SchwabConfirmedAdapter` requires one exact account match. It does not create
OAuth sessions or use a default callback. Existing local tokens are supplied by
the operator's own approved application. Snapshot code never interprets
`cashBalance`, `cashAvailableForTrading`, or buying power as settled funds.

The default adapter deliberately returns unverified settled cash and incomplete
open-order coverage: Schwab's order history query has a 60-day entry window,
which does not rule out older GTC orders. Before live use, the operator must
review the exact account's settled cash and ALL open orders at Schwab and supply
fresh `OperatorEvidence` (60-second lifetime). This is explicit human evidence,
not an API guarantee. Evidence must contain the actual full open-order results;
only an explicitly empty result can establish clear coverage. A reviewed flag
alone cannot do so. Any older GTC found during review blocks submission even if
absent from the API's limited window. No evidence is fabricated by the CLI.

The operator creates a separate `ConfirmedExecutor` allocation with only the
cash and whole-share lots assigned to this strategy. Manual holdings are never
imported. An `Approval` binds exact account, symbol, side, quantity, cent-precise
limit, reason, unique confirmation ID and a ten-minute expiry. Orders are DAY
limits in NORMAL hours. Every next order needs a fresh confirmation and account
snapshot. Existing orders or unexplained holdings shortages block submission.

Submission intent is fsynced before the broker POST. An exception leaves a
durable UNKNOWN state; restart cannot resend it. The operator first queries
broker history and supplies an order ID to `bind_uncertain`, which GET-verifies
the details and entry time. Ambiguity remains blocked. There is no automatic
cancel/replacement or retry. A rejected order needs a new human confirmation.

`refresh` reads broker execution legs, checks the sum against filledQuantity,
checks limit prices, and reconciles cumulative quantities/notional plus
operator-confirmed fees. Repeated snapshots are idempotent. A fee correction
without additional fills stops for explicit reconciliation rather than silently
altering the ledger. `confirm_settlement` requires evidence for the particular
sale; a calendar date alone cannot release live proceeds. These APIs are a
prepared integration boundary, not a validated unattended live trading service.

## Persistence and validation

State writes use a same-directory atomic replace, fsync and mode 0600. A POSIX
file lock prevents simultaneous paper/execution mutations. This operator tool
targets macOS/Linux. Schema and implementation fingerprints block silent paper
strategy changes. New checks replay only sessions after the persisted ledger;
they cannot rewrite earlier trades or move the clock backwards. A failed data
validation leaves the prior state intact. Research data and reports are cached
locally with source metadata and file hashes, and never added to Git.
Each research report also preserves its own `source_data` snapshot and hashes,
so subsequent paper data refreshes do not erase the report's provenance.

Run the four `tests/test_vti_sgov_*.py` files with dotenv disabled. Coverage
includes calendar boundaries, hysteresis, dividend entitlement, next-session
execution, cumulative partial fills, settlement, costs/latency, native-clock
parity, wrong accounts, expired confirmation, uncertain sends, restart safety
and paper idempotency. No live order is needed or permitted by these tests.
