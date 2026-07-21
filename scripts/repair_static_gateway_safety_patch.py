from pathlib import Path

path = Path("scripts/patch_static_gateway_safety_v2.py")
text = path.read_text(encoding="utf-8")

start_marker = '''replace_once(
    "local_gateway/service.py",
    """            WHERE user_id=? AND (
'''
end_marker = '''
replace_once(
    "local_gateway/routes.py",
'''
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise RuntimeError("Broken lease replacement block not found")

fixed = '''replace_once(
    "local_gateway/service.py",
    "            WHERE user_id=? AND (\\n"
    "                status='pending'\\n"
    "                OR (status='leased' AND datetime(lease_expires_at) <= datetime(?))\\n"
    "            )\\n"
    "            ORDER BY CASE action WHEN 'EXIT_POSITION' THEN 0 ELSE 1 END, id ASC\\n"
    "            LIMIT ?\\n"
    "            \\\"\\\"\\\",\\n"
    "            (gateway[\\\"user_id\\\"], now_iso, limit),\\n",
    "            WHERE user_id=?\\n"
    "              AND (?=1 OR action<>'PLACE_ENTRY')\\n"
    "              AND (\\n"
    "                status='pending'\\n"
    "                OR (status='leased' AND datetime(lease_expires_at) <= datetime(?))\\n"
    "              )\\n"
    "            ORDER BY CASE action WHEN 'EXIT_POSITION' THEN 0 ELSE 1 END, id ASC\\n"
    "            LIMIT ?\\n"
    "            \\\"\\\"\\\",\\n"
    "            (gateway[\\\"user_id\\\"], int(bool(allow_entries)), now_iso, limit),\\n",
    "lease entry filter",
)
'''

text = text[:start] + fixed + text[end:]
path.write_text(text, encoding="utf-8")
print("Static gateway safety patch generator repaired")
