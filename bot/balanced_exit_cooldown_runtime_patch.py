"""Force-apply balanced exit patch after Risk Control V2 has wrapped runtime."""

from bot import auto_portfolio_runtime as runtime
from bot.balanced_exit_cooldown_v4_patch import apply_balanced_exit_cooldown_v4_patch


def apply_balanced_exit_cooldown_runtime_patch():
    # Force one re-apply here so 4% BE, early danger exit and 12/20 minute
    # cooldown remain the final active runtime functions after Risk Control V2.
    runtime._okai_balanced_exit_cooldown_v4 = False
    apply_balanced_exit_cooldown_v4_patch()
