from pathlib import Path

path = Path('main.py')
text = path.read_text(encoding='utf-8')

imp = 'from subscription.razorpay_routes import router as razorpay_subscription_router\n'
if imp not in text:
    anchor = 'from subscription.routes import router as subscription_router\n'
    if anchor not in text:
        raise SystemExit('subscription import anchor not found')
    text = text.replace(anchor, anchor + imp, 1)

inc = 'app.include_router(razorpay_subscription_router)\n'
if inc not in text:
    anchor = 'app.include_router(subscription_router)\n'
    if anchor not in text:
        raise SystemExit('subscription include anchor not found')
    text = text.replace(anchor, anchor + inc, 1)

path.write_text(text, encoding='utf-8')
print('Razorpay subscription router wired')
