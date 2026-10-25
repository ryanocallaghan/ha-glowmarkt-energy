"""Check your Bright login and print yesterday's hourly usage.

Run with:  python3 scripts/check_api.py   (needs: pip install requests)
"""

from datetime import UTC, datetime, timedelta
import getpass

import requests

BASE = "https://api.glowmarkt.com/api/v0-1"
APP = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"

auth = requests.post(
    f"{BASE}/auth",
    headers={"applicationId": APP},
    json={"username": input("Bright email: "), "password": getpass.getpass("Bright password: ")},
    timeout=30,
).json()
if not auth.get("valid"):
    raise SystemExit(f"Login failed: {auth}")
headers = {"applicationId": APP, "token": auth["token"]}

end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
start = end - timedelta(days=1)
for resource in requests.get(f"{BASE}/resource", headers=headers, timeout=30).json():
    print(f"\n{resource['classifier']}  ({resource['resourceId']})")
    if not resource["classifier"].endswith(".consumption"):
        continue
    data = requests.get(
        f"{BASE}/resource/{resource['resourceId']}/readings",
        headers=headers,
        params={
            "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": (end - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": "PT1H",
            "function": "sum",
            "offset": "0",
        },
        timeout=30,
    ).json()["data"]
    for ts, value in data:
        print(f"  {datetime.fromtimestamp(ts, UTC):%Y-%m-%d %H:%M} UTC  {value:.3f} kWh")
    print(f"  total {sum(v or 0 for _, v in data):.2f} kWh")
