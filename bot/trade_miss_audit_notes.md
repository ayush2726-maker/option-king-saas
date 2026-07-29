# Trade Miss Audit Markers

The mobile Trade tab can show these warning markers after live scan refresh:

- `AUDIT`: candidate side, engine signal, display score and decision score.
- `BLOCK_REASON`: the first effective reason the engine blocked a trade.
- `MISSED_SIGNAL_AUDIT`: display/decision score was enough, but a gate still blocked.
- `TIME_GATE`: normal 14:45 fresh-entry cutoff is active.
- `STRONG_TREND_DAY`: VWAP, EMA, Supertrend and ADX are aligned for CE/PE.
- `FAILED_COMPONENTS`: enabled score components that did not pass for the selected side.

This is observability only. It does not change orders, risk, sizing, SL, cooldown or exits.
