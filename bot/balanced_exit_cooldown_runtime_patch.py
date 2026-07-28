"""Force-apply balanced exit patch after Risk Control V2 has wrapped runtime."""

from bot import auto_portfolio_runtime as runtime
from bot.balanced_exit_cooldown_patch import apply_balanced_exit_cooldown_patch


def apply_balanced_exit_cooldown_runtime_patch():
    # balanced_exit_cooldown_patch may be imported before Risk Control V2 finishes.
    # Force one re-apply here so 4% BE, early danger exit and 12/20 minute
    # cooldown remain the final active runtime functions.
    runtime._okai_balanced_exit_cooldown_v3 = False
    apply_balanced_exit_cooldown_patch()
