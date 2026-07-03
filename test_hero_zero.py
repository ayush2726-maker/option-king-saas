import requests

BASE = "https://option-king-saas-production.up.railway.app"
email = "ayush2726@gmail.com"
password = input("Password visible type karo: ").strip()

login = requests.post(BASE + "/auth/login", json={"email": email, "password": password}, timeout=30)
token = login.json().get("token") or ""
print("TOKEN_LEN:", len(token))
if not token:
    print(login.text[:500])
    raise SystemExit("Login failed")

h = {"Authorization": f"Bearer {token}"}

print("---- HERO ZERO START ----")
r = requests.post(
    BASE + "/bot/hero-zero/start",
    headers=h,
    json={"side": "PE"},
    timeout=30
)
print(r.status_code)
print(r.text[:2000])

print("---- SIGNAL ----")
r = requests.get(BASE + "/bot/signal", headers=h, timeout=30)
print(r.status_code)
print(r.text[:2000])

print("---- PAPER HISTORY ----")
r = requests.get(BASE + "/history/paper", headers=h, timeout=30)
print(r.status_code)
print(r.text[:2000])
