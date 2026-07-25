import json

from bot.adaptive_model import ensure_model_schema, maybe_train_models


if __name__ == "__main__":
    ensure_model_schema()
    print(json.dumps(maybe_train_models(force=True), indent=2))
