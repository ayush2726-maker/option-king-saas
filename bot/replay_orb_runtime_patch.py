"""Recover opening-range levels inside the replay-first AUTO runtime.

The AUTO portfolio engine does not use ``auto_portfolio_runtime._build_scan``
after ``live_scan_history_fallback_patch`` is installed.  It uses that module's
``_replay_scan`` path instead.  The old replay path explicitly wrote
``orb_high=0`` and ``orb_low=0`` even when the broker frame contained today's
09:15-09:30 candles, so the mobile breakdown always displayed ORB 0/11.

This patch is installed before the replay-first patch is activated.  It wraps the
actual frame collector and replay function used at runtime, performs a broker
backfill on that exact path, injects the recovered levels into market_data, and
exposes a marker in scan summaries for deployment diagnostics.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

PATCH_VERSION = "REPLAY_ORB_RUNTIME_V2"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if number == number else float(default)
    except Exception:
        return float(default)


def _orb_levels(frame) -> Tuple[float, float]:
    try:
        from bot.orb_session_backfill_patch import calculate_orb_levels_resilient

        high, low = calculate_orb_levels_resilient(frame)
        high = _f(high)
        low = _f(low)
        if high > 0 and low > 0 and high >= low:
            return high, low
    except Exception:
        pass
    return 0.0, 0.0


def _recover_frame(
    frame,
    broker_name: str,
    broker_obj,
    underlying: str,
    *,
    angel: bool,
):
    """Backfill ORB on the exact dataframe consumed by replay scoring."""
    from bot import angel_fetcher as legacy
    from bot.orb_session_backfill_patch import ensure_angel_orb, ensure_multi_orb

    name = str(underlying or "NIFTY").upper()
    if angel:
        return ensure_angel_orb(
            frame,
            broker_obj,
            legacy.INDEX_TOKENS[name],
            legacy.INDEX_EXCHANGE[name],
        )

    return ensure_multi_orb(
        frame,
        str(broker_name or "").lower(),
        broker_obj,
        name,
        upstox_keys=legacy.UPSTOX_INDEX_KEYS,
        zerodha_tokens=legacy.ZERODHA_INDEX_TOKENS,
    )


def _append_note(notes: Iterable[Any], value: str) -> list:
    result = list(notes or [])
    text = str(value or "").strip()
    if text and text not in result:
        result.append(text)
    return result


def apply_replay_orb_runtime_patch() -> None:
    """Patch the replay-first scan globals before main activates that runtime."""
    from bot import auto_portfolio_runtime as runtime
    from bot import live_scan_history_fallback_patch as replay

    if getattr(replay, "_okai_replay_orb_runtime_v2", False):
        return

    original_collect_frame = replay._collect_frame
    original_replay_scan = replay._replay_scan
    original_summary = runtime._summary

    def collect_frame_with_orb(
        user_id,
        broker_name,
        broker_obj,
        underlying,
        angel=False,
    ):
        frame, source, notes = original_collect_frame(
            user_id,
            broker_name,
            broker_obj,
            underlying,
            angel=angel,
        )

        before_high, before_low = _orb_levels(frame)
        recovered = frame
        recovery_error = None

        try:
            recovered = _recover_frame(
                frame,
                broker_name,
                broker_obj,
                underlying,
                angel=bool(angel),
            )
        except Exception as exc:
            recovery_error = f"{type(exc).__name__}:{str(exc)[:160]}"

        after_high, after_low = _orb_levels(recovered)
        available = bool(after_high > 0 and after_low > 0)
        recovered_now = bool(
            available
            and not (before_high > 0 and before_low > 0)
        )

        if available:
            frame = recovered
            marker = "ORB_RECOVERED" if recovered_now else "ORB_PRESENT"
            source = f"{source or 'BROKER_FRAME'}+{marker}"
            notes = _append_note(
                notes,
                (
                    f"orb_runtime={PATCH_VERSION} status={marker} "
                    f"high={after_high:.2f} low={after_low:.2f}"
                ),
            )
        else:
            notes = _append_note(
                notes,
                f"orb_runtime={PATCH_VERSION} status=UNAVAILABLE",
            )

        if recovery_error:
            notes = _append_note(
                notes,
                f"orb_backfill_error={recovery_error}",
            )

        return frame, source, notes

    def replay_scan_with_orb(
        user_id,
        underlying,
        frame,
        profile,
        source,
        notes,
    ):
        result = dict(
            original_replay_scan(
                user_id,
                underlying,
                frame,
                profile,
                source,
                notes,
            )
            or {}
        )

        orb_high, orb_low = _orb_levels(frame)
        available = bool(orb_high > 0 and orb_low > 0)

        market = dict(result.get("market_data") or {})
        market["orb_high"] = orb_high
        market["orb_low"] = orb_low
        market["orb_available"] = available
        market["orb_source"] = (
            "BROKER_SESSION_0915_0930"
            if available
            else "UNAVAILABLE"
        )
        market["orb_runtime_patch"] = PATCH_VERSION
        result["market_data"] = market

        signal = dict(result.get("signal_data") or {})
        warnings = list(signal.get("warnings") or [])
        warning = (
            "ORB_SESSION_RECOVERED_FOR_REPLAY"
            if available
            else "ORB_SESSION_UNAVAILABLE_AFTER_RETRY"
        )
        if warning not in warnings:
            warnings.append(warning)
        signal["warnings"] = warnings
        signal["orb_high"] = orb_high
        signal["orb_low"] = orb_low
        signal["orb_available"] = available
        signal["orb_runtime_patch"] = PATCH_VERSION
        result["signal_data"] = signal

        result["orb_high"] = orb_high
        result["orb_low"] = orb_low
        result["orb_available"] = available
        result["orb_runtime_patch"] = PATCH_VERSION
        return result

    def summary_with_orb(scan: Dict[str, Any]):
        summary = dict(original_summary(scan) or {})
        market = dict((scan or {}).get("market_data") or {})
        high = _f(market.get("orb_high"))
        low = _f(market.get("orb_low"))
        summary["orb_high"] = high
        summary["orb_low"] = low
        summary["orb_available"] = bool(high > 0 and low > 0)
        summary["orb_source"] = market.get("orb_source")
        summary["orb_runtime_patch"] = PATCH_VERSION
        return summary

    replay._collect_frame = collect_frame_with_orb
    replay._replay_scan = replay_scan_with_orb
    runtime._summary = summary_with_orb

    replay._okai_replay_orb_runtime_v2 = True
    runtime._okai_replay_orb_runtime_v2 = True
