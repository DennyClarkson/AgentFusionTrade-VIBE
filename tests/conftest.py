import copy
import time

import pytest

from fusion.store import Store
from fusion.engine import Engine
from fusion.config import DEFAULTS


def market():
    now = int(time.time()//300)*300
    bars = []
    for i in range(220):
        close = 2000 + i * .2
        bars.append({"time": now-(220-i)*300, "open": close-.1, "high": close+.1, "low": close-.2, "close": close, "volume": 100})
    bars[-1]["close"] += 1
    bars[-1]["high"] += 1
    price = bars[-1]["close"]
    return {"symbol": "XAUUSD", "tick": {"bid": price, "ask": price+.1, "time": time.time()}, "frames": {"M5": bars}, "captured_at": time.time(), "spec": {"point": .01, "digits": 2, "trade_tick_size": .01, "volume_min": .01, "volume_max": 100, "volume_step": .01, "trade_stops_level": 10, "trade_freeze_level": 0, "trade_contract_size": 100, "filling_mode": 2}}


class FakeBroker:
    def __init__(self):
        self.data = market()
        self.account_pin = None
        self.demo = True
        self.login = 123
        self.sent = []
        self.open_positions = []
        self.send_hook = None
        self.settings = {}

    def configure(self, settings): self.settings = settings
    def shutdown(self): pass
    def status(self):
        return {"connected": True, "error": None, "account": {"login": self.login, "server": "Demo", "currency": "USD", "balance": 3000, "equity": 3000, "free_margin": 3000, "trade_mode": 0 if self.demo else 2, "demo": self.demo, "trade_allowed": True}, "terminal": {"trade_allowed": True, "tradeapi_disabled": False}}
    def snapshot(self, strategy): return copy.deepcopy(self.data)
    def positions(self): return copy.deepcopy(self.open_positions)
    def orders(self): return []
    def deals_since(self, epoch): return []
    def loss_per_lot(self, symbol, side, entry, stop): return abs(entry-stop)*100
    def margin(self, symbol, side, volume, entry): return volume*1000
    def make_request(self, symbol, side, volume, entry, sl, tp, risk, comment):
        return {"symbol": symbol, "volume": volume, "price": entry, "sl": sl, "tp": tp, "magic": risk["magic"], "comment": comment}
    def check_order(self, request): return {"retcode": 0}
    def send_order(self, request):
        if self.send_hook: self.send_hook()
        self.sent.append(request)
        self.open_positions.append({"ticket": 42, "magic": request["magic"], "symbol": request["symbol"], "time": time.time(), "profit": 0})
        return {"retcode": 10009, "order": 42, "deal": 1}


@pytest.fixture
def configured(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    for category, values in {"strategy": {"context_timeframes": []}, "risk":{"sessions":[{"name":"fixture","start_utc":0,"end_utc":24,"risk_scale":1,"stop_scale":1,"target_scale":1}]}, "workflow": {"ai_gate": "off", "session_start_utc": 0, "session_end_utc": 0, "block_weekends": False}}.items():
        row = next(x for x in store.configs() if x["category"] == category)
        store.save(category, row["id"], row["name"], {**row["data"], **values}, row["version"])
    broker = FakeBroker()
    return store, broker, Engine(store, broker)
