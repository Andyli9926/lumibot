"""Research/paper/read-only commands. No live submit command is exposed."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="VTI/SGOV research and local paper workflow")
    parser.add_argument("command", choices=["research", "paper-init", "paper-check", "replay", "schwab-check"])
    parser.add_argument("--root", type=Path, default=Path(".lumibot/vti_sgov"))
    parser.add_argument("--capital", type=float, default=10000.)
    parser.add_argument("--start", default="2020-06-01")
    parser.add_argument("--refresh", action="store_true", help="Refresh public history from Yahoo")
    args = parser.parse_args()
    # Never implicitly load the repository's broker configuration in research.
    os.environ["LUMIBOT_DISABLE_DOTENV"] = "1"
    os.environ["IS_BACKTESTING"] = "true"
    os.environ["LUMIBOT_LAZY_CREDENTIALS"] = "true"
    os.environ["BACKTESTING_DATA_SOURCE"] = "none"
    os.environ.setdefault("LUMIBOT_CACHE_FOLDER", str(args.root / "cache"))
    from .core import Calendar
    from .execution import atomic_private
    from .paper import initialize, check, replay_exercise
    from .research import download_data, load_data, research_suite
    now = datetime.now(timezone.utc)
    args.root.mkdir(parents=True, exist_ok=True)
    if args.command == "paper-init":
        result = initialize(args.root / "paper.json", now, args.capital)
        print(json.dumps({k: result[k] for k in ("mode", "start", "capital", "review_at", "end_at")}, indent=2))
        return
    if args.command == "replay":
        result = replay_exercise()
        atomic_private(args.root / "replay_proof.json", result)
    elif args.command == "schwab-check":
        from schwab.auth import client_from_token_file
        from lumibot.brokers.schwab_confirmed import SchwabConfirmedAdapter
        # Credentials must be supplied explicitly by the operator's environment.
        logging.disable(logging.CRITICAL)
        client = client_from_token_file(os.environ["SCHWAB_TOKEN_PATH"],
            api_key=os.environ["SCHWAB_APP_KEY"], app_secret=os.environ["SCHWAB_APP_SECRET"])
        adapter = SchwabConfirmedAdapter(client, os.environ["SCHWAB_ACCOUNT_NUMBER"])
        view = adapter.snapshot()
        result = {"account_exact_match": True, "account_type": view.account_type,
            "open_order_count": len(view.open_orders), "snapshot_at": view.as_of.isoformat(),
            "settled_cash_verified": view.settled_cash is not None,
            "live_allocation": "not initialized by this command", "orders_submitted": 0}
    else:
        if args.refresh:
            data, end = download_data(args.root / "data", now)
        else:
            data = load_data(args.root / "data")
            end = Calendar(now.date() - timedelta(days=30), now.date()).completed(now)
        if args.command == "research":
            result = research_suite(data, args.start, end, args.root / "research", args.capital)
        else:
            result = check(args.root / "paper.json", data, now)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
