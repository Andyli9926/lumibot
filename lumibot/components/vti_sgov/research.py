"""Daily chronological research model. No broker credentials or real orders.

Raw opens execute prior decisions; ex-date dividends use opening holdings and
become spendable after an explicit payment-delay assumption. Adjusted closes
are used ONLY for signals. Portfolio reports come from the segregated ledger,
not the native LumiBot broker's unconstrained cash balance.
"""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .core import Calendar, Ledger, Rules, SYMBOLS, WEIGHTS, plan_orders


def validate_frames(data, start, end, lookback):
    cal = Calendar(start - timedelta(days=lookback * 2 + 60), end)
    sessions = [d for d in cal.sessions if start <= d <= end]
    if not sessions:
        raise ValueError("No sessions in research interval")
    for symbol in SYMBOLS:
        frame = data[symbol]
        if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
            raise ValueError(f"Duplicate/unsorted dates: {symbol}")
        expected = sessions
        if symbol == "VTI":
            earlier = [d for d in cal.sessions if d < start][-lookback:]
            expected = earlier + sessions
        if not set(expected).issubset(set(frame.index.date)):
            raise ValueError(f"Missing required sessions: {symbol}")
        selected = frame.loc[pd.to_datetime(expected)]
        values = selected[["open", "high", "low", "close", "adj_close"]].to_numpy()
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError(f"Invalid prices: {symbol}")
        if (selected.high < selected[["open", "close", "low"]].max(axis=1)).any() or (
                selected.low > selected[["open", "close", "high"]].min(axis=1)).any():
            raise ValueError(f"Invalid OHLC range: {symbol}")
        if not np.isfinite(selected.dividend).all() or (selected.dividend < 0).any():
            raise ValueError(f"Invalid dividend: {symbol}")
        if (selected.stock_splits != 0).any():
            raise ValueError("Stock split requires explicit accounting support")
    return cal, sessions


def load_data(folder):
    data = {}
    for symbol in SYMBOLS:
        frame = pd.read_csv(Path(folder) / f"{symbol}.csv", index_col=0, parse_dates=True)
        frame.columns = [c.lower().replace(" ", "_") for c in frame.columns]
        frame = frame.rename(columns={"dividends": "dividend"})
        if "capital_gains" in frame and (frame.capital_gains.fillna(0) != 0).any():
            raise ValueError("Capital gains distribution needs explicit accounting")
        frame.index = pd.DatetimeIndex(frame.index).tz_localize(None).normalize()
        data[symbol] = frame
    return data


def download_data(folder, now=None):
    """Public research data; fail closed if a ticker or completed bar is missing."""
    import yfinance as yf
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    end = Calendar(now.date() - timedelta(days=30), now.date()).completed(now)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(folder / "yf_cache"))
    try:
        raw = yf.download(list(SYMBOLS), start="2018-01-01",
                          end=(end + timedelta(days=1)).isoformat(),
                          auto_adjust=False, actions=True, group_by="ticker",
                          threads=False, progress=False)
        for symbol in SYMBOLS:
            frame = raw[symbol].dropna(subset=["Close"])
            if frame.empty or frame.index[-1].date() != end:
                raise ValueError(f"Missing latest completed bar for {symbol}: {end}")
            frame.to_csv(folder / f"{symbol}.csv")
    except Exception as exc:
        raise RuntimeError("Public history download failed; do not use stale signals") from exc
    provenance = {"source": "Yahoo Finance via yfinance", "version": yf.__version__,
        "downloaded_at": now.isoformat(), "last_completed_session": end.isoformat(),
        "auto_adjust": False, "dividend_policy": "raw price + separate distributions",
        "sha256": {s: hashlib.sha256((folder / f"{s}.csv").read_bytes()).hexdigest() for s in SYMBOLS}}
    (folder / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    return load_data(folder), end


class Engine:
    def __init__(self, data, start, end, *, capital=10_000., mode="trend", rules=None,
                 cost_bps=5., sell_fee=.10, delay_sessions=0, dividend_delay=5):
        self.start, self.end = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        self.rules = rules or Rules()
        self.cal, self.sessions = validate_frames(data, self.start, self.end, self.rules.lookback)
        if mode not in ("trend", "static", "buy_hold") or not 0 <= cost_bps < 1000:
            raise ValueError("Invalid portfolio mode/cost")
        if (type(delay_sessions) is not int or delay_sessions < 0
                or type(dividend_delay) is not int or dividend_delay < 1 or sell_fee < 0):
            raise ValueError("Invalid execution/payment assumption")
        self.data, self.capital, self.mode = data, capital, mode
        self.cost_bps, self.sell_fee = cost_bps, sell_fee
        self.delay_sessions, self.dividend_delay = delay_sessions, dividend_delay
        self.ledger = Ledger(capital)
        self.state = "defensive" if mode == "trend" else ("risk_on" if mode == "static" else "buy_hold")
        self.batch = {"due": self.sessions[min(delay_sessions, len(self.sessions) - 1)],
                      "reason": "initial_allocation", "signal_date": None}
        self.rows, self.trades, self.signals = [], [], []
        self.last_day = None
        self.batch_id = 0

    def step(self, day):
        expected = self.sessions[len(self.rows)] if len(self.rows) < len(self.sessions) else None
        if day != expected:
            raise ValueError(f"Non-chronological session: expected {expected}, got {day}")
        bars = {s: self.data[s].loc[pd.Timestamp(day)] for s in SYMBOLS}
        opening = {s: float(bars[s].open) for s in SYMBOLS}
        closing = {s: float(bars[s].close) for s in SYMBOLS}
        dividend_due = any(p["kind"] == "dividend" and date.fromisoformat(p["due"]) <= day
                           for p in self.ledger.pending)
        self.ledger.settle(day)
        # Opening lots held through the preceding close own this distribution.
        for s in SYMBOLS:
            dividend = float(bars[s].dividend) * self.ledger.lots[s]
            if dividend:
                self.ledger.credit_later(dividend, self.cal.next_session(day, self.dividend_delay), "dividend")
        if self.mode == "buy_hold" and dividend_due and self.batch is None:
            self.batch = {"due": day, "reason": "benchmark_dividend_reinvestment", "signal_date": None}
            self.batch_id += 1
        if self.batch is not None and day >= self.batch["due"]:
            orders = plan_orders(self.ledger, opening, self.state, self.batch["reason"],
                                 self.cost_bps, self.sell_fee,
                                 buy_only=self.batch.get("started", False))
            self.batch["started"] = True
            for intent in orders:
                sign = 1 if intent.side == "BUY" else -1
                price = opening[intent.symbol] * (1 + sign * self.cost_bps / 10_000)
                fee = self.sell_fee if intent.side == "SELL" else 0.
                oid = f"paper-{day}-{len(self.trades)}"
                self.ledger.apply_fill(oid, intent, intent.quantity, price * intent.quantity,
                                       fee, day, self.cal)
                self.trades.append({**asdict(intent), "date": day.isoformat(), "price": price,
                    "fee": fee, "spread_slippage": abs(price - opening[intent.symbol]) * intent.quantity,
                    "signal_date": self.batch["signal_date"], "batch": self.batch_id,
                    "adjusted_price": price * bars[intent.symbol].adj_close / bars[intent.symbol].close})
            # Do not drift-rebalance every day. Only finish this batch's deferred
            # buy after sale proceeds settle; dividends alone do not keep it open.
            if not any(p["kind"] == "sale" for p in self.ledger.pending):
                self.batch = None
        equity = self.ledger.value(closing)
        self.rows.append({"date": day.isoformat(), "equity": equity, "cash": self.ledger.cash,
                          "receivables": self.ledger.receivables, "state": self.state,
                          **{s: self.ledger.lots[s] for s in SYMBOLS}})
        if self.cal.weekly(day) and self.mode != "buy_hold":
            old = self.state
            if self.mode == "trend":
                history = self.data["VTI"].loc[:pd.Timestamp(day), "adj_close"]
                new, average = self.rules.signal(history, old)
            else:
                new, average = old, None
            weight = self.ledger.lots["VTI"] * closing["VTI"] / equity
            rebalance = self.rules.should_rebalance(old, new, self.cal.monthly(day), weight)
            self.signals.append({"date": day.isoformat(), "old": old, "state": new,
                "changed": old != new, "sma": average, "close": float(bars["VTI"].adj_close),
                "monthly": self.cal.monthly(day), "vti_weight": weight, "rebalance": rebalance})
            self.state = new
            if rebalance:
                self.batch_id += 1
                self.batch = {"due": self.cal.next_session(day, 1 + self.delay_sessions),
                    "reason": "trend_change" if old != new else "monthly_drift",
                    "signal_date": day.isoformat()}
        self.last_day = day

    def run(self):
        for day in self.sessions[len(self.rows):]:
            self.step(day)
        return self


def run_lumibot(engine, output):
    """LumiBot owns the daily clock; Engine replays each bar open-before-close.

    No native broker orders are sent: native cash settlement is deliberately not
    used. This is a custom accounting model integrated into a LumiBot Strategy.
    """
    from lumibot.backtesting import PandasDataBacktesting
    from lumibot.entities import Asset, Data
    from lumibot.example_strategies.vti_sgov_trend import VtiSgovResearch
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    pandas_data = {}
    for s in SYMBOLS:
        asset = Asset(s)
        frame = engine.data[s].loc[str(engine.start):str(engine.end),
                                   ["open", "high", "low", "close", "volume"]].copy()
        pandas_data[asset] = Data(asset, frame, timestep="day")
    _, strategy = VtiSgovResearch.run_backtest(
        # Pandas' daily clock advances to index > initial clock. Seed one day
        # before the first bar so the first funded session is not skipped.
        PandasDataBacktesting, datetime.combine(engine.start - timedelta(days=1), datetime.min.time()),
        datetime.combine(engine.end + timedelta(days=1), datetime.min.time()),
        parameters={"engine": engine}, pandas_data=pandas_data,
        budget=engine.capital, benchmark_asset=None, risk_free_rate=0., analyze_backtest=False,
        show_plot=False, show_tearsheet=False, save_tearsheet=False,
        show_indicators=False, save_logfile=False, show_progress_bar=False,
        quiet_logs=True, logfile=str(output / "lumibot.log"))
    driven = strategy.research_engine
    if [r["date"] for r in driven.rows] != [d.isoformat() for d in engine.sessions]:
        raise RuntimeError("LumiBot did not replay every expected session")
    # LumiBot can copy parameter objects. Expose the actual executed engine.
    engine.__dict__.update(driven.__dict__)
    return engine


def metrics(rows, trades, signals, capital):
    values = pd.Series([capital] + [r["equity"] for r in rows],
        index=pd.to_datetime([str(pd.Timestamp(rows[0]["date"]).date() - timedelta(days=1))]
                            + [r["date"] for r in rows]))
    years = (values.index[-1] - values.index[0]).days / 365.25
    peaks = values.cummax()
    dd = values / peaks - 1
    trough = dd.idxmin()
    peak = values.loc[:trough].idxmax()
    recovered = values.loc[trough:][values.loc[trough:] >= values.loc[peak]]
    unrecovered = bool(recovered.empty)
    recovery_end = values.index[-1] if unrecovered else recovered.index[0]
    rolling = {}
    for n in (3, 5):
        returns = []
        for stamp, value in values.items():
            before = stamp - pd.DateOffset(years=n)
            if before >= values.index[0]:
                prior = values.asof(before)
                returns.append((value / prior) ** (1 / n) - 1)
        rolling[f"rolling_{n}y_min"] = min(returns) if returns else None
        rolling[f"rolling_{n}y_max"] = max(returns) if returns else None
    last_exit, buybacks, expensive = None, 0, 0
    for trade in trades:
        if trade["symbol"] != "VTI" or trade["reason"] != "trend_change":
            continue
        if trade["side"] == "SELL":
            last_exit = trade["adjusted_price"]
        elif last_exit is not None:
            buybacks += 1
            expensive += trade["adjusted_price"] > last_exit
            last_exit = None
    return {"initial_capital": capital, "end_equity": float(values.iloc[-1]),
        "cagr": float((values.iloc[-1] / capital) ** (1 / years) - 1),
        "max_drawdown": float(dd.min()), "drawdown_peak": peak.date().isoformat(),
        "drawdown_trough": trough.date().isoformat(),
        "recovery_calendar_days": (recovery_end - peak).days, "unrecovered": unrecovered,
        "trade_count": len(trades), "trade_batches": len({t["batch"] for t in trades}),
        "trend_switches": sum(s["changed"] for s in signals),
        "annual_gross_turnover": sum(t["quantity"] * t["price"] for t in trades)
                                  / float(values.mean()) / years,
        "modeled_cost": sum(t["fee"] + t["spread_slippage"] for t in trades),
        "reentries": buybacks, "higher_price_reentries": int(expensive), **rolling}


def research_suite(data, start, end, output, capital=10_000.):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    # Preserve the exact input separately from the mutable daily paper cache.
    source = output / "source_data"
    source.mkdir(exist_ok=True)
    dataset_hashes = {}
    for symbol in SYMBOLS:
        path = source / f"{symbol}.csv"
        data[symbol].loc[:pd.Timestamp(end)].to_csv(path)
        dataset_hashes[symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
    code_hashes = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                   for name in ("core.py", "research.py")}
    configurations = [("buy_hold", {"mode": "buy_hold"}), ("static", {"mode": "static"}),
        ("trend", {}), ("trend_double_cost", {"cost_bps": 10., "sell_fee": .20}),
        ("trend_delay_1", {"delay_sessions": 1}), ("trend_delay_2", {"delay_sessions": 2})]
    configurations += [(f"ma{n}_band{b:g}", {"rules": Rules(n, b / 100)})
                       for n in (180, 200, 220) for b in (.5, 1., 1.5)
                       if (n, b) != (200, 1.)]
    results = {}
    for name, kwargs in configurations:
        engine = Engine(data, start, end, capital=capital, **kwargs)
        # Main portfolios genuinely use LumiBot's clock; sensitivity runs share
        # the identical deterministic engine verified by parity tests.
        if name in ("buy_hold", "static", "trend"):
            run_lumibot(engine, output / name)
        else:
            engine.run()
        results[name] = metrics(engine.rows, engine.trades, engine.signals, capital)
        pd.DataFrame(engine.rows).to_csv(output / f"{name}_equity.csv", index=False)
        pd.DataFrame(engine.trades).to_csv(output / f"{name}_trades.csv", index=False)
        pd.DataFrame(engine.signals).to_csv(output / f"{name}_signals.csv", index=False)
    summary = {"start": str(start), "end": str(end), "results": results,
        "created_at": datetime.now(timezone.utc).isoformat(), "dataset_hashes": dataset_hashes,
        "code_hashes": code_hashes, "main_rules": asdict(Rules()),
        "assumptions": {"capital": capital, "one_way_spread_plus_slippage_bps": 5,
            "online_etf_commission": 0, "modeled_sell_fee_dollars": .10,
            "sell_fee_note": "Illustrative allowance, NOT actual historical SEC/FINRA fees",
            "dividend_payment_delay_sessions": 5, "cash_interest": 0,
            "taxes": "excluded", "settlement": "historical T+2/T+1, conservative bank holidays",
            "execution": "next-session raw open plus costs; no limit-fill guarantee",
            "history": "Actual SGOV only; no proxy and no pre-inception crises",
            "benchmark": "Buy/hold reinvests distributions after modeled payment delay"}}
    (output / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
    lines = ["# VTI / SGOV research results", "", f"Window: {start} to {end}. Virtual capital: ${capital:,.0f}.", "",
        "| Portfolio | CAGR | Max drawdown | End equity | Trades | Modeled costs |",
        "|---|---:|---:|---:|---:|---:|"]
    for name, r in results.items():
        lines.append(f"| {name} | {r['cagr']:.2%} | {r['max_drawdown']:.2%} | ${r['end_equity']:,.2f} | {r['trade_count']} | ${r['modeled_cost']:.2f} |")
    lines += ["", "## Recovery and trading burden", "",
        "Recovery is peak-to-recovery for the maximum drawdown; an unrecovered episode is censored at the end.",
        "Gross turnover is all buy plus sell notional / average equity / years, including initial investment.", "",
        "| Portfolio | Recovery days | Unrecovered | Batches | Trend switches | Annual gross turnover | Higher-price reentries / reentries |",
        "|---|---:|---|---:|---:|---:|---:|"]
    for name in ("buy_hold", "static", "trend"):
        r = results[name]
        lines.append(f"| {name} | {r['recovery_calendar_days']} | {r['unrecovered']} | {r['trade_batches']} | {r['trend_switches']} | {r['annual_gross_turnover']:.1%} | {r['higher_price_reentries']} / {r['reentries']} |")
    lines += ["", "## Annualized rolling returns", "",
        "| Portfolio | 3-year minimum | 3-year maximum | 5-year minimum | 5-year maximum |",
        "|---|---:|---:|---:|---:|"]
    for name in ("buy_hold", "static", "trend"):
        r = results[name]
        fields = [r[f"rolling_{n}y_{bound}"] for n in (3, 5) for bound in ("min", "max")]
        lines.append("| " + name + " | " + " | ".join("N/A" if v is None else f"{v:.2%}" for v in fields) + " |")
    lines += ["", "## Assumptions and limitations", ""]
    lines += [f"- {k}: {v}" for k, v in summary["assumptions"].items()]
    lines += ["", "This is research, not an authorization to trade. Complete forward paper observation",
              "and independently reconcile live cash/settlement before considering actual orders.",
              "The short SGOV history excludes the 2008 crisis and the early-2020 crash."]
    lines += ["", "Sources: [Yahoo/yfinance](https://github.com/ranaroussi/yfinance),",
        "[SGOV issuer](https://www.ishares.com/us/products/314116/ishares-0-3-month-treasury-bond-etf),",
        "[Schwab pricing](https://www.schwab.com/legal/schwab-pricing-guide-for-individual-investors),",
        "[SEC T+1](https://www.sec.gov/exams/educationhelpguidesfaqs/t1-faq).",
        "An immutable-for-this-run source_data copy and input/code hashes in summary.json preserve the exact input.",
        "Refreshing the daily paper data cache does not change this research snapshot."]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    return summary
