"""Accuracy-first training patch for the shadow adaptive model.

The existing learner grew to many highly correlated interaction features.  This
patch keeps the final chronological holdout untouched while selecting a compact,
news-free champion on an *inner* chronological holdout.  It also anchors the
learner to the baseline probabilities and removes economically meaningless
one/two-rupee option moves from the training target.

Nothing in this module grants trade, blocking, sizing or order authority.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Mapping, Sequence


VERSION = "OKAI-ADAPTIVE-ACCURACY-V6"
MIN_USEFUL_NET_PNL_RUPEES = 100.0
MAX_USEFUL_NET_PNL_RUPEES = 500.0
MIN_INNER_VALIDATION_SAMPLES = 60


def _f(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _loads(model, value: Any, default: Any) -> Any:
    return model._loads(value, default)


def _candidate_direction(model, values: Sequence[float]) -> str:
    index = {name: position for position, name in enumerate(model.FEATURE_NAMES)}
    if (
        "setup_candidate_ce" in index
        and _f(values[index["setup_candidate_ce"]]) >= 0.5
    ):
        return "CE"
    if (
        "setup_candidate_pe" in index
        and _f(values[index["setup_candidate_pe"]]) >= 0.5
    ):
        return "PE"
    base = {
        label: _f(values[index[name]]) if name in index else 0.0
        for label, name in (
            ("CE", "base_ce"),
            ("PE", "base_pe"),
            ("NO_TRADE", "base_no_trade"),
        )
    }
    return max(base.items(), key=lambda item: item[1])[0]


def _economic_label(model, values: Sequence[float], stored: Mapping[str, Any]) -> str:
    """Label the action that was useful after all costs, not a tiny price tick."""
    ce_net = _f(stored.get("ce_net_pnl"))
    pe_net = _f(stored.get("pe_net_pnl"))
    details = _loads(model, stored.get("details_json"), {})
    ce_details = dict(details.get("ce") or {})
    pe_details = dict(details.get("pe") or {})
    ce_notional = _f(stored.get("ce_entry_price")) * max(
        1.0, _f(ce_details.get("quantity"), 1.0)
    )
    pe_notional = _f(stored.get("pe_entry_price")) * max(
        1.0, _f(pe_details.get("quantity"), 1.0)
    )
    useful = max(
        MIN_USEFUL_NET_PNL_RUPEES,
        min(MAX_USEFUL_NET_PNL_RUPEES, max(ce_notional, pe_notional) * 0.002),
    )
    candidate = _candidate_direction(model, values)
    if candidate in {"CE", "PE"}:
        candidate_net = ce_net if candidate == "CE" else pe_net
        opposite = "PE" if candidate == "CE" else "CE"
        opposite_net = pe_net if candidate == "CE" else ce_net
        if candidate_net >= useful:
            return candidate
        if opposite_net >= useful and opposite_net - candidate_net >= useful:
            return opposite
        return "NO_TRADE"
    best, best_net = max((("CE", ce_net), ("PE", pe_net)), key=lambda item: item[1])
    return best if best_net >= useful else "NO_TRADE"


def _base_probabilities(np, raw_values, model):
    positions = {name: index for index, name in enumerate(model.FEATURE_NAMES)}
    columns = [positions.get(name) for name in ("base_ce", "base_pe", "base_no_trade")]
    if any(index is None for index in columns):
        return np.full(
            (len(raw_values), len(model.LABELS)),
            1.0 / len(model.LABELS),
        )
    probabilities = np.maximum(0.0, raw_values[:, columns])
    totals = probabilities.sum(axis=1, keepdims=True)
    fallback = np.full_like(probabilities, 1.0 / len(model.LABELS))
    return np.divide(probabilities, totals, out=fallback, where=totals > 1e-9)


def _mix_probabilities(np, learned, baseline, blend: float, temperature: float):
    temperature = max(0.5, float(temperature))
    if abs(temperature - 1.0) > 1e-9:
        adjusted = np.power(np.maximum(learned, 1e-9), 1.0 / temperature)
        learned = adjusted / adjusted.sum(axis=1, keepdims=True)
    mixed = learned * (1.0 - blend) + baseline * blend
    return mixed / mixed.sum(axis=1, keepdims=True)


def _feature_candidates(model) -> Dict[str, list[int]]:
    names = list(model.FEATURE_NAMES)
    news = set(getattr(model, "NEWS_ABLATION_FEATURES", model.NEWS_FEATURES))

    def indices(selected: Iterable[str]) -> list[int]:
        wanted = set(selected)
        return [
            index
            for index, name in enumerate(names)
            if name in wanted and name not in news
        ]

    base_setup = [
        name for name in names
        if name.startswith("base_")
        or name.startswith("setup_")
        or name in {
            "adx", "rsi", "atr_percent", "volume_ratio",
            "india_vix", "india_vix_change",
        }
    ]
    option_market = base_setup + [
        name for name in names
        if name.startswith("option_")
        or name in {
            "coverage", "pcr", "oi_direction", "depth_imbalance",
            "spread_percent", "average_iv", "trend_strength",
            "liquidity_risk", "directional_edge",
        }
    ]
    stable_interactions = option_market + [
        name for name in names
        if name.startswith("base_option_")
        or name.startswith("ce_")
        or name.startswith("pe_")
        or name in {
            "direction_conflict", "volatility_pressure",
            "pcr_bullish_pressure", "pcr_bearish_pressure",
            "trend_momentum", "trend_exhaustion", "participation_strength",
            "derivatives_bull_pressure", "derivatives_bear_pressure",
            "volatility_expansion", "premium_expensive", "premium_asymmetry",
            "theta_risk_context", "no_trade_pressure",
        }
    ]
    candidates = {
        "BASE_SETUP_COMPACT": indices(base_setup),
        "OPTION_MARKET_COMPACT": indices(option_market),
        "STABLE_INTERACTIONS": indices(stable_interactions),
        "ALL_NO_NEWS": [i for i, name in enumerate(names) if name not in news],
    }
    return {name: value for name, value in candidates.items() if value}


def _select_champion(model, np, x_train, y_train, x_train_raw):
    inner_count = max(MIN_INNER_VALIDATION_SAMPLES, int(len(y_train) * 0.18))
    inner_count = min(inner_count, max(30, len(y_train) // 3))
    split = len(y_train) - inner_count
    x_fit, x_check = x_train[:split], x_train[split:]
    y_fit, y_check = y_train[:split], y_train[split:]
    base_check = _base_probabilities(np, x_train_raw[split:], model)
    best = None
    trials = []
    for candidate_name, selected in _feature_candidates(model).items():
        weights, bias = model._fit_softmax(np, x_fit[:, selected], y_fit)
        learned = model._predict_probabilities(np, x_check[:, selected], weights, bias)
        for blend in (0.0, 0.25, 0.50, 0.75, 1.0):
            for temperature in (0.8, 1.0, 1.25, 1.5):
                probabilities = _mix_probabilities(
                    np, learned, base_check, blend, temperature
                )
                accuracy, brier = model._metrics(np, probabilities, y_check)
                score = (1.0 - accuracy) + 0.20 * brier
                trial = {
                    "name": candidate_name,
                    "indices": selected,
                    "blend": blend,
                    "temperature": temperature,
                    "accuracy": accuracy,
                    "brier": brier,
                    "score": score,
                }
                trials.append(trial)
                # Prefer the simpler candidate when scores are effectively tied.
                tie = len(selected) / max(1, len(model.FEATURE_NAMES)) * 1e-5
                if best is None or score + tie < best[0]:
                    best = (score + tie, trial)
    selected = dict(best[1])
    selected["trials"] = [
        {
            "name": row["name"],
            "blend": row["blend"],
            "temperature": row["temperature"],
            "accuracy_percent": round(row["accuracy"] * 100.0, 2),
            "brier": round(row["brier"], 4),
        }
        for row in sorted(trials, key=lambda row: row["score"])[:8]
    ]
    return selected


def _install_training_rows(model) -> None:
    previous_rows = model._training_rows

    def accuracy_training_rows(horizon):
        x_rows, old_labels = previous_rows(horizon)
        conn = model.get_db()
        try:
            stored_rows = conn.execute(
                """SELECT s.created_at,s.symbol,o.best_label,o.ce_entry_price,
                o.pe_entry_price,o.ce_net_pnl,o.pe_net_pnl,o.details_json
                FROM ai_advanced_v2_snapshots s
                JOIN ai_advanced_v2_contract_outcomes o ON o.decision_id=s.id
                WHERE o.horizon_minutes=? AND o.best_label IN('CE','PE','NO_TRADE')
                  AND COALESCE(s.learning_eligible,1)=1
                  AND COALESCE(o.training_eligible,1)=1
                ORDER BY datetime(s.created_at),s.rowid""",
                (horizon,),
            ).fetchall()
        finally:
            conn.close()
        if len(stored_rows) != len(x_rows):
            return x_rows, old_labels

        label_index = {label: index for index, label in enumerate(model.LABELS)}
        output_x, output_y = [], []
        seen = set()
        relabelled = 0
        for values, old_label, stored_row in zip(x_rows, old_labels, stored_rows):
            stored = dict(stored_row)
            label = _economic_label(model, values, stored)
            old_name = model.LABELS[int(old_label)]
            relabelled += int(label != old_name)
            # One shared market event can be captured for several SaaS users.
            # Count that market state once instead of pretending it is new data.
            minute = str(stored.get("created_at") or "")[:16]
            key = (
                str(stored.get("symbol") or ""),
                minute,
                label,
                tuple(round(_f(value), 6) for value in values),
            )
            if key in seen:
                continue
            seen.add(key)
            output_x.append(list(values))
            output_y.append(label_index[label])
        model.ACCURACY_V6_DATASET_DIAGNOSTICS[int(horizon)] = {
            "raw_rows": len(x_rows),
            "unique_market_rows": len(output_x),
            "economically_relabelled_rows": relabelled,
            "minimum_useful_net_pnl_rupees": MIN_USEFUL_NET_PNL_RUPEES,
        }
        return output_x, output_y

    model._training_rows = accuracy_training_rows


def _install_trainer(model) -> None:
    def train_horizon_v6(horizon=15):
        model.ensure_model_schema()
        try:
            import numpy as np
        except Exception as exc:
            model._collecting(horizon, 0, f"NUMPY_UNAVAILABLE:{exc}")
            return {
                "success": False,
                "status": "COLLECTING",
                "reason": "NUMPY_UNAVAILABLE",
            }

        x_rows, y_rows = model._training_rows(horizon)
        sample_count = len(y_rows)
        if sample_count < model.MIN_TRAINING_SAMPLES:
            model._collecting(horizon, sample_count)
            return {
                "success": True,
                "status": "COLLECTING",
                "display_status": model._display_status("COLLECTING", sample_count),
                "sample_count": sample_count,
                "required": model.MIN_TRAINING_SAMPLES,
            }

        x = np.asarray(x_rows, dtype=float)
        y = np.asarray(y_rows, dtype=int)
        validation_count = max(model.MIN_VALIDATION_SAMPLES, int(sample_count * 0.20))
        split = sample_count - validation_count
        x_train_raw, x_valid_raw = x[:split], x[split:]
        y_train, y_valid = y[:split], y[split:]

        means = x_train_raw.mean(axis=0)
        scales = x_train_raw.std(axis=0)
        scales = np.where(scales < 1e-8, 1.0, scales)
        x_train = (x_train_raw - means) / scales
        x_valid = (x_valid_raw - means) / scales

        champion = _select_champion(model, np, x_train, y_train, x_train_raw)
        selected = champion["indices"]
        selected_weights, selected_bias = model._fit_softmax(
            np, x_train[:, selected], y_train
        )
        full_weights = np.zeros((x_train.shape[1], len(model.LABELS)))
        full_weights[selected, :] = selected_weights
        learned = model._predict_probabilities(np, x_valid[:, selected], selected_weights, selected_bias)
        base_valid = _base_probabilities(np, x_valid_raw, model)
        probabilities = _mix_probabilities(
            np, learned, base_valid, champion["blend"], champion["temperature"]
        )
        accuracy, brier = model._metrics(np, probabilities, y_valid)

        train_counts = np.bincount(y_train, minlength=len(model.LABELS))
        valid_counts = np.bincount(y_valid, minlength=len(model.LABELS))
        majority = int(np.argmax(train_counts))
        majority_accuracy = float((y_valid == majority).mean())
        base_accuracy = float((base_valid.argmax(axis=1) == y_valid).mean())
        baseline = max(majority_accuracy, base_accuracy)

        # News remains a challenger/report only.  It cannot become champion
        # from one noisy period and therefore cannot reduce live shadow quality.
        all_indices = list(range(len(model.FEATURE_NAMES)))
        all_weights, all_bias = model._fit_softmax(
            np, x_train[:, all_indices], y_train
        )
        all_news_probabilities = model._predict_probabilities(
            np, x_valid, all_weights, all_bias
        )
        all_news_accuracy, all_news_brier = model._metrics(
            np, all_news_probabilities, y_valid
        )

        diagnostics = model._feature_diagnostics(
            np,
            full_weights,
            train_counts,
            valid_counts,
            accuracy,
            baseline,
            brier,
            accuracy,
            brier,
        )
        news_delta = round((all_news_accuracy - accuracy) * 100.0, 2)
        diagnostics["news_effect"] = {
            "usefulness": (
                "HELPFUL_CHALLENGER_ONLY"
                if news_delta >= 1.0
                else "HARMFUL"
                if news_delta <= -1.0
                else "NEUTRAL_UNPROVEN"
            ),
            "validation_accuracy_with_news_percent": round(all_news_accuracy * 100.0, 2),
            "validation_accuracy_without_news_percent": round(accuracy * 100.0, 2),
            "accuracy_delta_percentage_points": news_delta,
            "brier_with_news": round(all_news_brier, 4),
            "brier_without_news": round(brier, 4),
            "champion_uses_news": False,
            "meaning": "News is report-only until it proves stable across repeated chronological windows.",
        }
        diagnostics["accuracy_v6"] = {
            "selected_candidate": champion["name"],
            "selected_feature_count": len(selected),
            "total_feature_count": len(model.FEATURE_NAMES),
            "baseline_blend": champion["blend"],
            "temperature": champion["temperature"],
            "inner_validation_trials": champion["trials"],
            "majority_baseline_accuracy_percent": round(majority_accuracy * 100.0, 2),
            "strategy_baseline_accuracy_percent": round(base_accuracy * 100.0, 2),
            "dataset": dict(model.ACCURACY_V6_DATASET_DIAGNOSTICS.get(int(horizon), {})),
            "outer_holdout_was_not_used_for_model_selection": True,
        }
        status = "ACTIVE_SHADOW" if diagnostics["activation_gate"]["passed"] else "VALIDATION_FAILED"

        # Temperature is folded into the stored logits.  Baseline blending is
        # stored in diagnostics and applied by the patched predictor.
        stored_weights = full_weights / max(0.5, champion["temperature"])
        stored_bias = selected_bias / max(0.5, champion["temperature"])
        conn = model.get_db()
        try:
            conn.execute(
                """UPDATE ai_adaptive_models_v2 SET model_version=?,status=?,trained_at=?,
                sample_count=?,train_count=?,validation_count=?,validation_accuracy=?,baseline_accuracy=?,
                brier_score=?,feature_names_json=?,means_json=?,scales_json=?,weights_json=?,bias_json=?,
                diagnostics_json=?,last_error=NULL WHERE horizon_minutes=?""",
                (
                    model.VERSION, status, model._iso(), sample_count,
                    len(y_train), len(y_valid), round(accuracy, 6),
                    round(baseline, 6), round(brier, 6),
                    model._dumps(model.FEATURE_NAMES), model._dumps(means.tolist()),
                    model._dumps(scales.tolist()), model._dumps(stored_weights.tolist()),
                    model._dumps(stored_bias.tolist()), model._dumps(diagnostics), horizon,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "success": True,
            "status": status,
            "display_status": model._display_status(status, sample_count),
            "sample_count": sample_count,
            "validation_accuracy_percent": round(accuracy * 100.0, 2),
            "baseline_accuracy_percent": round(baseline * 100.0, 2),
            "brier_score": round(brier, 4),
            "diagnostics": diagnostics,
        }

    model.train_horizon = train_horizon_v6


def _install_predictor(model) -> None:
    original_predict = model.predict_adaptive

    def predict_v6(feature: Mapping[str, Any], horizon: int = 15):
        result = original_predict(feature, horizon)
        if not result.get("available"):
            return result
        diagnostics = dict(result.get("diagnostics") or {})
        accuracy = dict(diagnostics.get("accuracy_v6") or {})
        blend = _f(accuracy.get("baseline_blend"))
        if blend <= 0:
            return result
        learned = dict(result.get("probabilities") or {})
        base = {
            "CE": max(0.0, _f(feature.get("base_ce"))),
            "PE": max(0.0, _f(feature.get("base_pe"))),
            "NO_TRADE": max(0.0, _f(feature.get("base_no_trade"))),
        }
        base_total = sum(base.values())
        if base_total <= 0:
            return result
        mixed = {
            label: (
                (1.0 - blend) * _f(learned.get(label))
                + blend * (base[label] / base_total * 100.0)
            )
            for label in model.LABELS
        }
        rounded = {label: int(round(mixed[label])) for label in model.LABELS}
        rounded["NO_TRADE"] += 100 - sum(rounded.values())
        decision, confidence = max(rounded.items(), key=lambda item: item[1])
        result["probabilities"] = rounded
        result["decision"] = decision
        result["confidence"] = confidence
        result["accuracy_model"] = accuracy.get("selected_candidate")
        return result

    model.predict_adaptive = predict_v6


def apply_adaptive_accuracy_v6_patch() -> bool:
    try:
        from bot import adaptive_model_v2 as model
        if getattr(model, "ADAPTIVE_ACCURACY_V6_APPLIED", False):
            return True
        model.ACCURACY_V6_DATASET_DIAGNOSTICS = {}
        _install_training_rows(model)
        _install_trainer(model)
        _install_predictor(model)
        model.VERSION = f"{model.VERSION}+{VERSION}"
        model.ADAPTIVE_ACCURACY_V6_APPLIED = True
        return True
    except Exception:
        return False


__all__ = [
    "MIN_USEFUL_NET_PNL_RUPEES",
    "VERSION",
    "_economic_label",
    "_feature_candidates",
    "_mix_probabilities",
    "apply_adaptive_accuracy_v6_patch",
]
