"""Ensure Advanced AI uses the final patched adaptive feature builder."""
from __future__ import annotations


def apply_adaptive_runtime_binding_v3() -> bool:
    try:
        from bot import adaptive_model_v2 as model
        from bot import advanced_intelligence_v2 as advanced
        advanced.feature_vector = model.feature_vector
        # ``advanced_intelligence_v2`` may be imported by an earlier runtime
        # patch. Rebind callable references as well, otherwise it keeps the
        # pre-patch predictor even though V6 trained and stored the new model.
        advanced.predict_adaptive = model.predict_adaptive
        advanced.maybe_train_models = model.maybe_train_models
        return True
    except Exception:
        return False


__all__ = ["apply_adaptive_runtime_binding_v3"]
