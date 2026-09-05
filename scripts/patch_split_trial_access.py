from pathlib import Path

p = Path('main.py')
s = p.read_text(encoding='utf-8')

imp = 'from subscription.trial_access_middleware import TrialAccessMiddleware\n'
anchor = 'from subscription.entitlement_routes import router as entitlement_router\n'
if imp not in s:
    if anchor not in s:
        raise SystemExit('entitlement router import anchor missing')
    s = s.replace(anchor, anchor + imp, 1)

mw = 'app.add_middleware(TrialAccessMiddleware)\n'
anchor2 = 'app.add_middleware(TradeLiveRuntimeRecoveryMiddleware)\n'
if mw not in s:
    if anchor2 not in s:
        raise SystemExit('middleware anchor missing')
    s = s.replace(anchor2, anchor2 + mw, 1)

s = s.replace('RELEASE_VERSION = "split-trial-entitlements-v1-20260905"', 'RELEASE_VERSION = "split-trial-enforced-v2-20260905"')
p.write_text(s, encoding='utf-8')
print('patched main.py split trial access')
