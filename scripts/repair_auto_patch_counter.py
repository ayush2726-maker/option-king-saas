from pathlib import Path

path = Path("scripts/patch_auto_only_sensex_v1.py")
text = path.read_text(encoding="utf-8")
old = '''backtest, count = volume_pattern.subn(add_volume_available, backtest)
if count < 3:
    raise RuntimeError(f"expected at least 3 market_data volume blocks, found {count}")
'''
new = '''backtest, multiline_count = volume_pattern.subn(add_volume_available, backtest)

single_line_pattern = re.compile(
    r'(?P<indent>\\s*)"volume_ratio": float\\(last\\["VOL_RATIO"\\]\\),'
)

def add_single_line_volume_available(match):
    indent = match.group("indent")
    return (
        f'{indent}"volume_ratio": float(last["VOL_RATIO"]),\\n'
        f'{indent}"volume_available": bool(\\n'
        f'{indent}    last.get("VOLUME_AVAILABLE", True)\\n'
        f'{indent}),' 
    )

backtest, single_line_count = single_line_pattern.subn(
    add_single_line_volume_available,
    backtest,
)
count = multiline_count + single_line_count
if count < 3:
    raise RuntimeError(
        f"expected at least 3 market_data volume blocks, found {count} "
        f"(multiline={multiline_count}, single={single_line_count})"
    )
'''
if old not in text:
    raise RuntimeError("counter block marker not found")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("AUTO patch counter repaired")
