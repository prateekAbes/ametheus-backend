import re

with open("app/api/v1/endpoints/admin.py", "r", encoding="utf-8") as f:
    text = f.read()

for m in re.finditer(r'@router\.(post|patch|put|get)\(["\']([^"\']+)["\']', text):
    route = m.group(2)
    if any(k in route.lower() for k in ["pay", "wallet", "deposit", "ledger", "payout"]):
        print(m.group(0))
