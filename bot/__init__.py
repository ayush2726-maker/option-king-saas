"""Option King bot package runtime patches."""

# Keep this import tiny and fail-closed so local scripts/tests that do not configure
# broker credentials still start normally.  The patch itself is monitoring-only and
# only fills Angel PCR when chain-derived PCR is missing.
try:
    from bot.angel_pcr_recovery_patch import apply_angel_pcr_recovery_patch

    apply_angel_pcr_recovery_patch()
except Exception:
    pass
