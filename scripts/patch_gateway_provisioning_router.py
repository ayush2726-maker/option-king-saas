from pathlib import Path

p = Path('main.py')
s = p.read_text()
import_line = 'from local_gateway.provisioning_routes import router as gateway_provisioning_router\n'
if import_line not in s:
    anchor = 'from local_gateway.routes import router as local_gateway_router\n'
    if anchor not in s:
        raise SystemExit('local gateway import anchor not found')
    s = s.replace(anchor, anchor + import_line, 1)
include_line = 'app.include_router(gateway_provisioning_router)\n'
if include_line not in s:
    anchor = 'app.include_router(local_gateway_router)\n'
    if anchor not in s:
        raise SystemExit('local gateway include anchor not found')
    s = s.replace(anchor, anchor + include_line, 1)
s = s.replace('RELEASE_VERSION = "split-trial-enforced-v2-20260905"', 'RELEASE_VERSION = "aws-auto-gateway-provisioning-v1-20260905"')
p.write_text(s)
print('gateway provisioning router patched')
