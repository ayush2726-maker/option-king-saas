from pathlib import Path
import re


def replace_once(path, old, new, label):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"{label} marker not found in {path}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# Angel/NSE index candles often have no usable volume. Preserve the fact that
# volume is unavailable, but keep VOL_RATIO numerically neutral instead of 0.
replace_once(
    "bot/angel_fetcher.py",
    '''    df["VOL_RATIO"] = (
        safe_volume / valid_volume_ma
    ).fillna(0.0)
''',
    '''    df["VOLUME_AVAILABLE"] = cumulative_volume > 0
    df["VOL_RATIO"] = (
        safe_volume / valid_volume_ma
    ).where(
        df["VOLUME_AVAILABLE"],
        1.0,
    ).fillna(1.0)
''',
    "neutral index volume ratio",
)

strategy_path = Path("bot/strategy.py")
strategy = strategy_path.read_text(encoding="utf-8")

if "VOLUME_NEUTRAL_BONUS" not in strategy:
    strategy = strategy.replace(
        "VOLUME_RATIO_THRESHOLD = 1.2\n",
        "VOLUME_RATIO_THRESHOLD = 1.2\nVOLUME_NEUTRAL_BONUS = 7  # half-weight when index volume is unavailable\n",
        1,
    )

strategy = strategy.replace(
    '''    gap_day: bool = False,
) -> dict:
''',
    '''    gap_day: bool = False,
    volume_available: bool = True,
) -> dict:
''',
    1,
)
strategy = strategy.replace(
    '''    # Volume confirmation
    if volume_ratio > 0:
        if volume_ratio >= VOLUME_RATIO_THRESHOLD:
            volume_bonus = min(15, int((volume_ratio - 1.0) * 10))
            score += volume_bonus
        else:
            warnings.append(f"VOLUME_LOW:{volume_ratio:.2f}x")
''',
    '''    # Volume confirmation. NSE index candles may legitimately have no
    # volume. Treat missing volume as neutral half-weight rather than a hard
    # scoring disadvantage; never invent a high volume ratio.
    if not volume_available:
        volume_bonus = VOLUME_NEUTRAL_BONUS
        score += volume_bonus
        warnings.append("VOLUME_UNAVAILABLE_NEUTRAL")
    elif volume_ratio > 0:
        if volume_ratio >= VOLUME_RATIO_THRESHOLD:
            volume_bonus = min(15, int((volume_ratio - 1.0) * 10))
            score += volume_bonus
        else:
            warnings.append(f"VOLUME_LOW:{volume_ratio:.2f}x")
''',
    1,
)
strategy = strategy.replace(
    '''        "volume_bonus": volume_bonus,
        "mtf_bonus": mtf_bonus,
''',
    '''        "volume_bonus": volume_bonus,
        "volume_available": bool(volume_available),
        "mtf_bonus": mtf_bonus,
''',
    1,
)

# Custom profiles receive half of their configured volume weight when the
# data source cannot provide index volume.
custom_volume_marker = '''    volume_ratio = float(
        market_data.get("volume_ratio", 0)
    )
'''
if custom_volume_marker not in strategy:
    raise RuntimeError("custom volume marker not found")
strategy = strategy.replace(
    custom_volume_marker,
    custom_volume_marker + '''    volume_available = bool(
        market_data.get(
            "volume_available",
            volume_ratio > 0,
        )
    )
''',
    1,
)
strategy = strategy.replace(
    '''    volume_bonus = 0
    if enabled.get("volume", True):
        volume_threshold = float(
            profile.get(
                "volume_threshold",
                1.2,
            )
        )
        if (
            volume_ratio > 0
            and volume_ratio >= volume_threshold
        ):
            volume_bonus = int(
                weights.get("volume", 0)
            )
            score += volume_bonus
        else:
            warnings.append(
                f"VOLUME_LOW:{volume_ratio:.2f}x"
            )
''',
    '''    volume_bonus = 0
    if enabled.get("volume", True):
        volume_threshold = float(
            profile.get(
                "volume_threshold",
                1.2,
            )
        )
        if not volume_available:
            volume_bonus = max(
                0,
                int(round(float(weights.get("volume", 0)) * 0.5)),
            )
            score += volume_bonus
            warnings.append("VOLUME_UNAVAILABLE_NEUTRAL")
        elif (
            volume_ratio > 0
            and volume_ratio >= volume_threshold
        ):
            volume_bonus = int(
                weights.get("volume", 0)
            )
            score += volume_bonus
        else:
            warnings.append(
                f"VOLUME_LOW:{volume_ratio:.2f}x"
            )
''',
    1,
)
# Add availability to custom result (the second volume_bonus result block).
custom_result_marker = '''        "volume_ratio": volume_ratio,
        "volume_bonus": volume_bonus,
        "mtf_confirmed": mtf_ok,
'''
if custom_result_marker not in strategy:
    raise RuntimeError("custom result marker not found")
strategy = strategy.replace(
    custom_result_marker,
    '''        "volume_ratio": volume_ratio,
        "volume_available": bool(volume_available),
        "volume_bonus": volume_bonus,
        "mtf_confirmed": mtf_ok,
''',
    1,
)

# Protected default profile.
default_volume_marker = '''    volume_ratio = float(market_data.get("volume_ratio", 1.0))
'''
if default_volume_marker not in strategy:
    raise RuntimeError("default volume marker not found")
strategy = strategy.replace(
    default_volume_marker,
    default_volume_marker + '''    volume_available = bool(
        market_data.get(
            "volume_available",
            volume_ratio > 0,
        )
    )
''',
    1,
)
strategy = strategy.replace(
    '''    tqu = calculate_tqu_score(
        base["base_score"], adx, volume_ratio,
        mtf_ok, is_sideways, gap_day,
    )
''',
    '''    tqu = calculate_tqu_score(
        base["base_score"], adx, volume_ratio,
        mtf_ok, is_sideways, gap_day,
        volume_available=volume_available,
    )
''',
    1,
)
default_result_marker = '''        "volume_ratio": volume_ratio,
        "volume_bonus": tqu["volume_bonus"],
        "mtf_confirmed": mtf_ok,
'''
if default_result_marker not in strategy:
    raise RuntimeError("default result marker not found")
strategy = strategy.replace(
    default_result_marker,
    '''        "volume_ratio": volume_ratio,
        "volume_available": bool(volume_available),
        "volume_bonus": tqu["volume_bonus"],
        "mtf_confirmed": mtf_ok,
''',
    1,
)
strategy_path.write_text(strategy, encoding="utf-8")

# Backtest must explicitly pass availability and expose per-index diagnostics.
backtest_path = Path("backtest/routes.py")
backtest = backtest_path.read_text(encoding="utf-8")

volume_pattern = re.compile(
    r'(?P<indent>\s*)"volume_ratio": float\(\n'
    r'(?P<body>\s*last\["VOL_RATIO"\]\n\s*\),)'
)

def add_volume_available(match):
    indent = match.group("indent")
    original = f'{indent}"volume_ratio": float(\n{match.group("body")}'
    return original + (
        f'\n{indent}"volume_available": bool(\n'
        f'{indent}    last.get("VOLUME_AVAILABLE", True)\n'
        f'{indent}),'
    )

backtest, count = volume_pattern.subn(add_volume_available, backtest)
if count < 3:
    raise RuntimeError(f"expected at least 3 market_data volume blocks, found {count}")

score_detail_marker = '''            "volume_ratio": round(
                signal_data.get("volume_ratio", 0),
                2,
            ),
            "volume_bonus": signal_data.get("volume_bonus", 0),
'''
if score_detail_marker not in backtest:
    raise RuntimeError("score detail volume marker not found")
backtest = backtest.replace(
    score_detail_marker,
    '''            "volume_ratio": round(
                signal_data.get("volume_ratio", 0),
                2,
            ),
            "volume_available": bool(
                signal_data.get("volume_available", True)
            ),
            "volume_bonus": signal_data.get("volume_bonus", 0),
''',
    1,
)

result_marker = '''        "debug_score_count": len(_score_log),
        "debug_scores_over_60": sum(
'''
if result_marker not in backtest:
    raise RuntimeError("single-index diagnostics marker not found")
backtest = backtest.replace(
    result_marker,
    '''        "debug_score_count": len(_score_log),
        "debug_candle_count": len(df),
        "debug_volume_available": bool(
            full_indicator_df["VOLUME_AVAILABLE"].any()
            if "VOLUME_AVAILABLE" in full_indicator_df.columns
            else True
        ),
        "debug_volume_neutral_count": sum(
            1
            for row in _score_detail_log
            if not row.get("volume_available", True)
        ),
        "debug_scores_over_60": sum(
''',
    1,
)

selected_marker = '''        selected.append(trade)
        busy_until = exit_time

    raw_auto = {
'''
if selected_marker not in backtest:
    raise RuntimeError("AUTO selected marker not found")
backtest = backtest.replace(
    selected_marker,
    '''        selected.append(trade)
        busy_until = exit_time

    selected_counts = {
        symbol: sum(
            1 for trade in selected
            if trade.get("instrument") == symbol
        )
        for symbol in _OKAI_AUTO_INSTRUMENTS
    }

    raw_auto = {
''',
    1,
)

per_instrument_marker = '''                "max_score": (
                    result.get(
                        "debug_max_score"
                    )
                    if isinstance(result, dict)
                    else None
                ),
            }
'''
if per_instrument_marker not in backtest:
    raise RuntimeError("AUTO per-instrument marker not found")
backtest = backtest.replace(
    per_instrument_marker,
    '''                "max_score": (
                    result.get(
                        "debug_max_score"
                    )
                    if isinstance(result, dict)
                    else None
                ),
                "candles": (
                    result.get("debug_candle_count", 0)
                    if isinstance(result, dict)
                    else 0
                ),
                "volume_available": (
                    result.get("debug_volume_available")
                    if isinstance(result, dict)
                    else None
                ),
                "volume_neutral_count": (
                    result.get("debug_volume_neutral_count", 0)
                    if isinstance(result, dict)
                    else 0
                ),
                "selected_trades": selected_counts.get(instrument, 0),
            }
''',
    1,
)

backtest_path.write_text(backtest, encoding="utf-8")
print("AUTO-only-SENSEX volume-neutral patch applied")
