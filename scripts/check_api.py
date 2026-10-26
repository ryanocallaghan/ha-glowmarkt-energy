"""Check your Bright login and print yesterday's hourly usage.

Run with:  python3 scripts/check_api.py   (no extra packages needed)
"""

from datetime import UTC, datetime, timedelta
import getpass
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = "https://api.glowmarkt.com/api/v0-1"
APP = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"


def call(path, headers, params=None, body=None):
    url = f"{BASE}{path}" + (f"?{urlencode(params)}" if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, headers={**headers, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except HTTPError as err:
        raise SystemExit(f"{path} failed: HTTP {err.code} {err.read().decode()[:200]}")


auth = call(
    "/auth",
    {"applicationId": APP},
    body={"username": input("Bright email: "), "password": getpass.getpass("Bright password: ")},
)
if not auth.get("valid"):
    raise SystemExit(f"Login failed: {auth}")
headers = {"applicationId": APP, "token": auth["token"]}

end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
start = end - timedelta(days=1)
for resource in call("/resource", headers):
    print(f"\n{resource['classifier']}  ({resource['resourceId']})")
    if not resource["classifier"].endswith(".consumption"):
        continue
    data = call(
        f"/resource/{resource['resourceId']}/readings",
        headers,
        {
            "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "to": (end - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            "period": "PT1H",
            "function": "sum",
            "offset": "0",
        },
    )["data"]
    for ts, value in data:
        print(f"  {datetime.fromtimestamp(ts, UTC):%Y-%m-%d %H:%M} UTC  {value or 0:.3f} kWh")
    print(f"  total {sum(v or 0 for _, v in data):.2f} kWh")
