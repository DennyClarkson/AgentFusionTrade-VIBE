"""Explicit, one-shot demo gateway smoke: minimum lot with SL/TP, then immediate close.

This tests execution connectivity, NOT strategy performance. Preview by default.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fusion.broker import MT5Broker
from fusion.engine import Engine
from fusion.store import Store, uid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--account-login", type=int)
    args = parser.parse_args()
    import httpx
    try:
        state = httpx.get("http://127.0.0.1:8787/api/status", timeout=10, trust_env=False).json()
        if any(state["engine"][x] for x in ("running", "busy", "armed")):
            raise RuntimeError("Stop and disarm the workbench before smoke testing")
    except httpx.ConnectError:
        pass
    store, broker = Store(ROOT / "data/fusion.sqlite3"), MT5Broker()
    engine = Engine(store, broker)
    report = {"purpose": "demo execution smoke, not strategy validation", "started_at": time.time()}
    try:
        cfg, versions = engine.configs()
        account = broker.status()["account"]
        if not account.get("demo") or account.get("currency") != "USD":
            raise RuntimeError("USD demo account required")
        if broker.positions() or broker.orders() or store.unresolved():
            raise RuntimeError("Smoke requires flat account, no pending orders or unresolved intents")
        snap = broker.snapshot(cfg["strategy"])
        volume = snap["spec"]["volume_min"]
        budget = min(4.0, cfg["risk"]["capital"]*cfg["risk"]["risk_per_trade_pct"]/100*.8)
        entry = snap["tick"]["ask"]
        # Derive USD/price sensitivity from MT5 instead of assuming contract size.
        per_price = broker.loss_per_lot(snap["symbol"], "BUY", entry, entry-1)
        distance = budget/(per_price*volume)
        signal = {"action": "BUY", "entry": entry, "sl": entry-distance, "tp": entry+distance, "reason": "explicit integration smoke"}
        report.update(volume=volume, budget=budget, signal=signal, config_versions=versions)
        if not args.execute:
            print(json.dumps({**report, "preview": True}, ensure_ascii=False))
            return
        if args.account_login != account["login"]:
            raise RuntimeError("Explicit current demo login required")
        engine.arm(args.account_login)
        cfg["workflow"]["mode"] = "demo"
        checked = engine.guards(snap, signal, cfg)
        if not checked["allowed"]:
            raise RuntimeError("Smoke guards refused: " + ";".join(checked["reasons"]))
        request = broker.make_request(snap["symbol"], "BUY", volume, entry, signal["sl"], signal["tp"], cfg["risk"], "fusion smoke")
        risk_estimate = (broker.loss_per_lot(snap["symbol"], "BUY", request["price"]+cfg["risk"]["max_slippage_points"]*snap["spec"]["point"], request["sl"])+cfg["risk"]["commission_per_lot"])*volume
        if risk_estimate > cfg["risk"]["capital"]*cfg["risk"]["risk_per_trade_pct"]/100:
            raise RuntimeError("Normalized test risk exceeds configured budget")
        preflight = broker.check_order(request)
        if preflight["retcode"] != 0:
            raise RuntimeError("Broker preflight refused: " + preflight.get("comment", ""))
        key = "smoke:" + uid()
        store.intent(key, {"request": request, "account": broker.account_pin})
        result = broker.send_order(request)
        report.update(open=result, estimated_risk=risk_estimate)
        owned = [p for p in broker.positions() if p["magic"] == cfg["risk"]["magic"]]
        ownership = store.get("managed_demo_accounts", {})
        ownership[f"{account['login']}|{account['server']}"] = owned
        store.resolve(key, "done" if result["retcode"] == 10009 else "unknown", {"request": request, "result": result, "account": broker.account_pin}, {"managed_demo_accounts": ownership})
        report["close"] = []
        for p in owned:
            report["close"].append(engine.close_demo(p["ticket"]))
        report["remaining_owned"] = len([p for p in broker.positions() if p["magic"] == cfg["risk"]["magic"]])
        deals = broker.deals_since(int(report["started_at"])-10)
        relevant = [d for d in deals if d.get("magic") == cfg["risk"]["magic"]]
        report["realized_pnl"] = sum(sum(d.get(k, 0) for k in ("profit", "commission", "swap", "fee")) for d in relevant)
        report["deal_times"] = [{"raw_time": d["time"], "raw_age_seconds": round(time.time()-d["time"], 2), "corrected_age_seconds": round(time.time()-d["time"]+broker.offset, 2)} for d in relevant]
        if report["remaining_owned"] or result["retcode"] != 10009:
            raise RuntimeError("Smoke not fully reconciled; inspect persisted report and terminal")
        report["ok"] = True
    except Exception as exc:
        report.update(ok=False, error=str(exc))
        raise
    finally:
        engine.stop()
        report["finished_at"] = time.time()
        store.audit("demo_smoke", report)
        (ROOT / "artifacts").mkdir(exist_ok=True)
        (ROOT / "artifacts/demo-smoke.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        broker.shutdown()


if __name__ == "__main__":
    main()
