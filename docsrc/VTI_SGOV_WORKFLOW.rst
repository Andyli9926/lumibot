VTI / SGOV weekly workflow
=========================

Description: Run the fixed ETF trend research and local paper observation.
Last Updated: 2026-09-30
Status: Research and paper; no automatic live trading
Audience: Strategy operator

Overview
--------

The portfolio starts defensive. At the close of the last NYSE session of each
week, compare VTI's adjusted close with its 200-session average. Enter at or
above 101% of the average; exit at or below 99%; retain state inside the band.
The targets are 80% VTI / 18% SGOV / 2% cash when invested, and 0% / 98% / 2%
when defensive. Whole shares leave additional residual cash.

Execute no earlier than the next trading session. Ordinary rebalancing is
checked only at the first weekly checkpoint of each month, and only when VTI
deviates by at least five percentage points. A state change bypasses that gate.
There is no fixed take-profit or buy-the-dip rule.

Run research
------------

From the repository with its Python environment installed:

.. code-block:: bash

   export LUMIBOT_DISABLE_DOTENV=1
   export LUMIBOT_CACHE_FOLDER="$PWD/.lumibot/cache"
   export IS_BACKTESTING=true
   python -m lumibot.components.vti_sgov research --refresh
   python -m lumibot.components.vti_sgov replay

The default virtual capital is $10,000. ``--capital`` changes virtual research
capital only. The default start is 2020-06-01, the first actual SGOV market bar
in this data source. No pre-inception proxy is silently substituted.
``.lumibot/vti_sgov/research/REPORT.md`` contains three portfolios, doubled costs,
one/two-session extra confirmation delays, and a small 180/200/220-day and
0.5/1/1.5-percent parameter grid. Parameters are sensitivity checks, not an
optimization recommendation. CSVs preserve equity, signals and trades.

Results use a custom settlement-aware ledger driven by LumiBot's daily clock.
The price source is Yahoo via yfinance. Signals use adjusted closes; accounting
uses raw prices and dividends once. Dividend cash payment dates and sell fees
are modeled assumptions. The default is five sessions after the ex-date, 5 bps
one-way spread/slippage, and a $0.10 sell-fee allowance. Taxes are excluded.
See ``docs/VTI_SGOV_ENGINEERING.md`` for exact definitions and limitations.

Forward paper observation
-------------------------

.. code-block:: bash

   python -m lumibot.components.vti_sgov paper-init
   python -m lumibot.components.vti_sgov paper-check --refresh

Initialization schedules the first virtual allocation for the next NYSE
session after creation, never a past opening. Start once; an existing state
cannot be overwritten. Daily checks process only fully closed sessions and
simulate the prior signal at a subsequent opening with modeled costs. They do
not contact Schwab or place orders. A check during a trading day excludes that
day. Repeated checks do not duplicate trades. Private state lives in
``.lumibot/vti_sgov/paper.json`` and the latest concise result in
``paper_status.json``. Keep these files out of Git and backed up privately.

The paper period has a four-week review and an eight-week boundary. It cannot
be completed instantly by a historical backtest. The synthetic ``replay``
exercise separately covers purchase, partial sale, duplicate fills, settlement
waiting and defensive re-entry. A daily check can be scheduled after the US
close; weekly signals still follow the exchange calendar, including holidays.

Schwab and confirmation
----------------------

``schwab-check`` is read-only. Explicitly supply ``SCHWAB_APP_KEY``,
``SCHWAB_APP_SECRET``, ``SCHWAB_TOKEN_PATH`` and ``SCHWAB_ACCOUNT_NUMBER`` through
your private environment. It verifies one exact CASH account and reports
whether settlement/open-order coverage is verified; it does not allocate funds.
Never paste credentials or commit the private environment or token file.

There is deliberately no live-submit CLI. The separate confirmation API needs
an explicit strategy allocation, current broker evidence of settled cash and
all open orders, and a human review of each exact DAY limit order. Manual
holdings of the same ETF remain outside the strategy ledger. A submit timeout
must be investigated at the broker before another send. Calendar T+1 is only
an estimate; the particular sale must be confirmed settled before reuse.

After 4–8 weeks, review signals, hypothetical fills, actual spreads, settlement
records and operating failures. Decide the live allocation and integration
validation separately. Paper returns and backtest drawdowns do not guarantee
future returns or a maximum loss.
