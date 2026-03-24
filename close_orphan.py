"""
Close orphaned 71000 position + extra perp leg
"""
import requests
import yaml

with open("/root/Deribit_arbitrage_bot/secrets.yaml") as f:
    cfg = yaml.safe_load(f)

BASE = "https://test.deribit.com/api/v2"
auth = requests.get(BASE + "/public/auth", params={
    "grant_type":    "client_credentials",
    "client_id":     cfg["deribit"]["client_id"],
    "client_secret": cfg["deribit"]["client_secret"],
}).json()
token = auth["result"]["access_token"]
headers = {"Authorization": "Bearer " + token}

# 先確認目前倉位
pos_resp = requests.get(BASE + "/private/get_positions",
    params={"currency": "BTC"}, headers=headers).json()
positions = {p["instrument_name"]: p for p in pos_resp["result"]}

print("Current positions:")
for name, p in positions.items():
    if p["size"] != 0:
        print("  {:42s} size={:>10}  dir={}".format(name, p["size"], p["direction"]))

# 沖銷 71000 選擇權兩腿
to_close = ["BTC-25MAR26-71000-C", "BTC-25MAR26-71000-P"]
for inst in to_close:
    pos = positions.get(inst)
    if not pos or pos["size"] == 0:
        print("SKIP {} (size=0 or not found)".format(inst))
        continue
    resp = requests.get(BASE + "/private/close_position", params={
        "instrument_name": inst,
        "type": "market",
    }, headers=headers).json()
    if "result" in resp:
        order = resp["result"].get("order", {})
        print("OK {} order_id={} state={}".format(inst, order.get("order_id"), order.get("order_state")))
    else:
        print("FAIL {}: {}".format(inst, resp.get("error")))

# 沖銷多餘的 perp（69500 的 perp = 20930，總共 41940，多了 21010）
perp_pos = positions.get("BTC-PERPETUAL")
if perp_pos:
    total_perp = abs(perp_pos["size"])   # 41940
    known_perp = 20930                   # 69500 position
    orphan_perp = total_perp - known_perp  # 21010
    print("Perp total={} known={} orphan={}".format(total_perp, known_perp, orphan_perp))
    if orphan_perp > 0:
        # 目前是 short perp，沖銷要 buy
        resp = requests.get(BASE + "/private/buy", params={
            "instrument_name": "BTC-PERPETUAL",
            "amount":          orphan_perp,
            "type":            "market",
        }, headers=headers).json()
        if "result" in resp:
            order = resp["result"].get("order", {})
            print("OK BTC-PERPETUAL (buy {}) order_id={} state={}".format(
                orphan_perp, order.get("order_id"), order.get("order_state")))
        else:
            print("FAIL BTC-PERPETUAL: {}".format(resp.get("error")))

print("Done.")
