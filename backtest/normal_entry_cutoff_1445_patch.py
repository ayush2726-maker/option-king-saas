"""Backtest-only 14:45 Normal entry cutoff experiment.

This patch changes the existing Normal-entry cutoff used inside the historical
signal loop from 15:00 to 14:45 IST. It does not post-filter completed trades,
so quantity, daily five-trade selection, compounding and P&L remain internally
consistent.

Existing positions may continue until their normal exit. Combined expiry days
keep their stricter 14:25 Hero Zero reservation. Paper and Live are unchanged.
"""

from backtest import routes


NORMAL_ENTRY_CUTOFF_MINUTES = 14 * 60 + 45


def apply_normal_entry_cutoff_1445_patch():
    if getattr(routes, "_okai_normal_entry_cutoff_1445_v2", False):
        return

    # The native backtest loop reads this constant before allowing a new
    # Normal entry. Changing it here blocks the signal itself instead of
    # deleting already simulated trades afterwards.
    routes._OKAI_NORMAL_ENTRY_CUTOFF_MINUTES = NORMAL_ENTRY_CUTOFF_MINUTES
    routes._okai_normal_entry_cutoff_1445_v1 = True
    routes._okai_normal_entry_cutoff_1445_v2 = True
