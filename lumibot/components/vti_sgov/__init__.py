"""Weekly VTI/SGOV research and segregated paper accounting (no auto trading)."""

from .core import Calendar, Intent, Ledger, Rules, plan_orders

__all__ = ["Calendar", "Intent", "Ledger", "Rules", "plan_orders"]
