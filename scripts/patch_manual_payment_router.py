from pathlib import Path

path = Path('main.py')
text = path.read_text(encoding='utf-8')

import_line = 'from subscription.manual_payment_routes import router as manual_payment_router\n'
if import_line not in text:
    anchor = 'from subscription.razorpay_routes import router as razorpay_subscription_router\n'
    if anchor not in text:
        anchor = 'from subscription.routes import router as subscription_router\n'
    text = text.replace(anchor, anchor + import_line, 1)

include_line = 'app.include_router(manual_payment_router)\n'
if include_line not in text:
    anchor = 'app.include_router(razorpay_subscription_router)\n'
    if anchor not in text:
        anchor = 'app.include_router(subscription_router)\n'
    text = text.replace(anchor, anchor + include_line, 1)

path.write_text(text, encoding='utf-8')
print('manual payment router wired')
