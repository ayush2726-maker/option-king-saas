"""Compatibility utilities for the calibrated advanced model.

The live broker adapters remain in bot.advanced_intelligence V1 and are reused
by V2 so Angel One, Upstox and Zerodha stay on the same capability layer.
"""
from bot.advanced_intelligence import clamp, num, side as direction

__all__ = ["clamp", "num", "direction"]
