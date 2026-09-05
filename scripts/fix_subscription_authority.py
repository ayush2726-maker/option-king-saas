from pathlib import Path

path = Path('main.py')
text = path.read_text()

old_import = 'from bot.eod_safety_testing_access_patch import TestingFullAccessAndFreshDataMiddleware, apply_eod_entry_guard_patch, initialize_testing_access_and_cleanup'
new_import = 'from bot.eod_safety_testing_access_patch import apply_eod_entry_guard_patch, cleanup_invalid_eod_paper_entries, cleanup_requested_admin_trade_dates'
if old_import in text:
    text = text.replace(old_import, new_import)

text = text.replace('app.add_middleware(TestingFullAccessAndFreshDataMiddleware)\n', '')

old_startup = '''    testing_init = initialize_testing_access_and_cleanup()\n    print(f"Testing access/EOD cleanup | users={testing_init['testing_access_users_updated']} | removed={testing_init['invalid_eod_paper_trades_removed']}")\n'''
new_startup = '''    requested_cleanup = cleanup_requested_admin_trade_dates()\n    invalid_eod_removed = cleanup_invalid_eod_paper_entries()\n    print(f"Subscription authority enabled | testing_access_users_updated=0 | invalid_eod_removed={invalid_eod_removed} | requested_cleanup_removed={requested_cleanup['removed']}")\n'''
if old_startup not in text:
    raise SystemExit('startup testing-access block not found')
text = text.replace(old_startup, new_startup)

if 'TestingFullAccessAndFreshDataMiddleware' in text:
    raise SystemExit('testing access middleware still referenced')
if 'initialize_testing_access_and_cleanup' in text:
    raise SystemExit('testing access initializer still referenced')

path.write_text(text)
print('Subscription authority patch applied: no global auto-activation or response rewriting')
