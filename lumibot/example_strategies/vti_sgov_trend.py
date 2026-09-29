"""LumiBot clock adapter for the settlement-aware VTI/SGOV research engine."""
from lumibot.strategies.strategy import Strategy


class VtiSgovResearch(Strategy):
    def initialize(self, engine):
        if not self.is_backtesting:
            raise ValueError("VtiSgovResearch cannot run against a live broker")
        self.sleeptime = "1D"
        self.set_market("NYSE")
        self.research_engine = engine

    def on_trading_iteration(self):
        day = self.get_datetime().date()
        engine = self.research_engine
        if day in engine.sessions and day != engine.last_day:
            engine.step(day)
