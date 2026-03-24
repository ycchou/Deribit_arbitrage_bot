import requests
import yaml

with open("/root/Deribit_arbitrage_bot/secrets.yaml") as f:
    cfg = yaml.safe_load(f)

CLIENT_ID     = cfg["deribit"]["client_id"]
CLIENT_SECRET = cfg["deribit"]["client_secret"]
BASE = "https://test.deribit.com/api/v2"

auth = requests.get(BASE + "/public/auth", params={
    "grant_type":    "client_credentials",
    "client_id":     CLIENT_ID,
    "client_secret": CLIENT_SECRET,
}).json()

if "error" in auth:
    print("Auth failed:", auth["error"])
    exit(1)

token = auth["result"]["access_token"]
headers = {"Authorization": "Bearer " + token}

resp = requests.get(BASE + "/private/get_positions",
    params={"currency": "BTC"}, headers=headers).json()
positions = resp.get("result", [])

print("Found {} open positions:".format(len(positions)))
for p in positions:
    name = p["instrument_name"]
    size = p["size"]
    direction = p["direction"]
    pnl = p.get("floating_profit_loss_usd", 0)
    print("  {:42s} size={:>10}  dir={:5s}  pnl=${:.2f}".format(name, size, direction, pnl))

print("\nClosing all with market orders...")
for p in positions:
    instrument = p["instrument_name"]
    size = abs(p["size"])
    if size == 0:
        print("  SKIP {} (size=0)".format(instrument))
        continue

    close_resp = requests.get(BASE + "/private/close_position", params={
        "instrument_name": instrument,
        "type":            "market",
    }, headers=headers).json()

    if "result" in close_resp:
        order = close_resp["result"].get("order", {})
        print("  OK {} order_id={} state={}".format(
            instrument, order.get("order_id"), order.get("order_state")))
    else:
        print("  FAIL {}: {}".format(instrument, close_resp.get("error")))

print("Done.")
