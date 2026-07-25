"""Loader for broker-neutral advanced intelligence V2 implementation parts.

The implementation is split only to keep individual source files reviewable.
All parts execute in one module namespace and expose the public V2 API.
"""
from pathlib import Path

_BASE = Path(__file__).resolve().parent
for _part_name in (
    "advanced_intelligence_1.inc",
    "advanced_intelligence_2.inc",
    "advanced_intelligence_3.inc",
    "advanced_intelligence_4.inc",
    "advanced_intelligence_5.inc",
    "advanced_intelligence_6.inc",
):
    _path = _BASE / _part_name
    exec(
        compile(_path.read_text(encoding="utf-8"), str(_path), "exec"),
        globals(),
        globals(),
    )
del _BASE, _part_name, _path
