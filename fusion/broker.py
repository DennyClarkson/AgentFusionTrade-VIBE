"""Single serialized boundary to the local MT5 terminal. Real accounts cannot send."""
import math
import threading
import time
from datetime import datetime, timezone


class BrokerError(RuntimeError):
    pass


class OrderNotSubmitted(BrokerError):
    """A final local preflight rejected before entering the MT5 send boundary."""


def plain(value):
    if hasattr(value, "_asdict"):
        return {k: plain(v) for k, v in value._asdict().items()}
    if isinstance(value, (tuple, list)):
        return [plain(x) for x in value]
    return value


class MT5Broker:
    def __init__(self, module=None):
        if module is None:
            try:
                import MetaTrader5 as module
            except ImportError:
                module = None
        self.m = module
        self.lock = threading.RLock()
        self.settings = {"terminal_path": "", "server_utc_offset_hours": 0}
        self.account_pin = None
        self.protection_limits = {}

    def configure(self, settings):
        with self.lock:
            self.settings = dict(settings)

    @property
    def offset(self):
        return self.settings["server_utc_offset_hours"] * 3600

    def connect(self):
        if self.m is None:
            raise BrokerError("MetaTrader5 Python 包不可用，需要 Windows 本地运行")
        args = [self.settings["terminal_path"]] if self.settings["terminal_path"] else []
        if not self.m.initialize(*args, timeout=10000):
            raise BrokerError(f"MT5 初始化失败：{self.m.last_error()}")

    def _need(self, value, label):
        if value is None:
            raise BrokerError(f"{label}失败：{self.m.last_error()}")
        return value

    def status(self):
        with self.lock:
            try:
                self.connect()
                a = self._need(self.m.account_info(), "账户读取")
                t = self._need(self.m.terminal_info(), "终端读取")
                return {"connected": bool(t.connected), "error": None, "account": {
                    "login": a.login, "server": a.server, "currency": a.currency, "balance": a.balance,
                    "equity": a.equity, "free_margin": a.margin_free, "trade_mode": a.trade_mode,
                    "demo": a.trade_mode == self.m.ACCOUNT_TRADE_MODE_DEMO, "trade_allowed": a.trade_allowed,
                }, "terminal": {"trade_allowed": t.trade_allowed, "tradeapi_disabled": t.tradeapi_disabled},
                    "server_utc_offset_hours": self.settings["server_utc_offset_hours"]}
            except Exception as exc:
                return {"connected": False, "error": str(exc), "account": {}, "terminal": {}}

    def _guard(self):
        status = self.status()
        a, t = status["account"], status["terminal"]
        if not status["connected"] or not a.get("demo"):
            raise BrokerError("执行仅允许已连接的模拟账户")
        if self.account_pin != (a["login"], a["server"]):
            raise BrokerError("模拟账户未解锁或账户已切换")
        if not a["trade_allowed"] or not t["trade_allowed"] or t["tradeapi_disabled"]:
            raise BrokerError("MT5 或账户未允许自动交易")

    def snapshot(self, strategy):
        with self.lock:
            self.connect()
            m, symbol = self.m, strategy["symbol"]
            spec = self._need(m.symbol_info(symbol), f"品种 {symbol}")
            if not spec.visible and not m.symbol_select(symbol, True):
                raise BrokerError("品种无法加入 Market Watch")
            tick = self._need(m.symbol_info_tick(symbol), "报价读取")
            frames = {}
            for tf in dict.fromkeys([strategy["timeframe"], *strategy["context_timeframes"]]):
                count=strategy.get("history_bars",{}).get(tf,strategy["bars"])
                rates = self._need(m.copy_rates_from_pos(symbol, getattr(m, f"TIMEFRAME_{tf}"), 1, count), "K 线读取")
                if len(rates) == 0:
                    raise BrokerError(f"{tf} 无闭合行情，请在 MT5 加载历史")
                frames[tf] = [{"time": int(r["time"]) - self.offset, "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"]), "volume": int(r["tick_volume"]), "spread": int(r["spread"])} for r in rates]
            fields = ["point", "digits", "trade_tick_size", "volume_min", "volume_max", "volume_step", "trade_stops_level", "trade_freeze_level", "trade_contract_size", "filling_mode", "trade_mode"]
            return {"symbol": symbol, "tick": {"bid": tick.bid, "ask": tick.ask, "time": tick.time - self.offset, "raw_time": tick.time},
                    "spec": {k: getattr(spec, k) for k in fields}, "frames": frames, "captured_at": time.time(), "time_offset_hours": self.offset / 3600, "source": "MT5 / closed bars"}

    def positions(self):
        with self.lock:
            self.connect()
            rows = self._need(self.m.positions_get(), "持仓读取")
            keys = ["ticket", "symbol", "type", "volume", "price_open", "sl", "tp", "profit", "swap", "magic", "time", "comment"]
            return [{**{k: getattr(r, k) for k in keys}, "time": r.time - self.offset} for r in rows]

    def orders(self):
        with self.lock:
            self.connect()
            return plain(self._need(self.m.orders_get(), "挂单读取"))

    def deals_since(self, epoch):
        with self.lock:
            self.connect()
            start = datetime.fromtimestamp(epoch + self.offset, timezone.utc)
            end = datetime.fromtimestamp(time.time() + self.offset + 60, timezone.utc)
            return plain(self._need(self.m.history_deals_get(start, end), "成交历史读取"))

    def loss_per_lot(self, symbol, side, entry, stop):
        with self.lock:
            self.connect()
            value = self._need(self.m.order_calc_profit(self.m.ORDER_TYPE_BUY if side == "BUY" else self.m.ORDER_TYPE_SELL, symbol, 1.0, entry, stop), "每手风险计算")
            if not math.isfinite(value) or value >= 0:
                raise BrokerError("止损方向错误或合约风险计算无效")
            return -value

    def margin(self, symbol, side, volume, entry):
        with self.lock:
            self.connect()
            value = self._need(self.m.order_calc_margin(self.m.ORDER_TYPE_BUY if side == "BUY" else self.m.ORDER_TYPE_SELL, symbol, volume, entry), "保证金计算")
            if not math.isfinite(value) or value < 0:
                raise BrokerError("保证金结果无效")
            return value

    def make_request(self, symbol, side, volume, entry, sl, tp, risk, comment):
        with self.lock:
            self.connect()
            m = self.m
            s = self._need(m.symbol_info(symbol), "合约读取")
            # IOC/FOK are bit flags on symbol; RETURN unsupported for market execution.
            filling = m.ORDER_FILLING_IOC if s.filling_mode & 2 else m.ORDER_FILLING_FOK if s.filling_mode & 1 else m.ORDER_FILLING_RETURN
            tick_size = s.trade_tick_size or s.point
            normalize = lambda p: round(round(p / tick_size) * tick_size, s.digits)
            return {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume), "type": m.ORDER_TYPE_BUY if side == "BUY" else m.ORDER_TYPE_SELL,
                    "price": normalize(entry), "sl": normalize(sl), "tp": normalize(tp), "deviation": risk["max_slippage_points"], "magic": risk["magic"],
                    "comment": comment[:31], "type_time": m.ORDER_TIME_GTC, "type_filling": filling}

    def check_order(self, request):
        with self.lock:
            self.connect()
            self._guard()
            return plain(self._need(self.m.order_check(request), "订单预检"))

    def send_order(self, request):
        with self.lock:
            try:
                self.connect()
                self._guard()
                if request.get("action")==getattr(self.m,"TRADE_ACTION_SLTP",6):
                    signature=(self.account_pin,request["position"],request["sl"],request["tp"])
                    limit=self.protection_limits.pop(signature,None)
                    if limit is None: raise BrokerError("保护修改缺少已验证报价时限")
                    self.prepare_protection(request["position"],request["sl"],request["tp"],request["magic"],limit)
                    self.protection_limits.pop(signature,None)
            except Exception as exc:
                raise OrderNotSubmitted(str(exc)) from exc
            # Never retry an ambiguous send; caller persists intent before entering.
            result = self.m.order_send(request)
            if result is None:
                raise BrokerError("订单结果不确定，必须核对终端成交；禁止自动重试")
            return plain(result)

    def prepare_protection(self, ticket, sl, tp, magic, max_age=30):
        with self.lock:
            self._guard()
            p=next((p for p in self.positions() if p["ticket"]==ticket),None)
            if not p or p["magic"]!=magic: raise BrokerError("保护修改仅允许本框架持仓")
            tick=self._need(self.m.symbol_info_tick(p["symbol"]),"保护报价")
            if not -5<=time.time()-(tick.time-self.offset)<=max_age: raise BrokerError("保护修改报价过期")
            spec=self._need(self.m.symbol_info(p["symbol"]),"保护规格")
            sign=1 if p["type"]==self.m.POSITION_TYPE_BUY else -1
            price=tick.bid if sign==1 else tick.ask
            step=spec.trade_tick_size or spec.point
            sl=round(round(sl/step)*step,spec.digits);tp=round(round(tp/step)*step,spec.digits)
            minimum=max(spec.trade_stops_level,spec.trade_freeze_level)*spec.point
            if not all(math.isfinite(v) and v>0 for v in (sl,tp)) or p["sl"]<=0 or (sl-p["sl"])*sign<step*.9:
                raise BrokerError("禁止取消、放宽或重复修改止损")
            if (price-sl)*sign<minimum or (tp-price)*sign<minimum: raise BrokerError("保护价进入经纪商冻结／最小距离")
            self.protection_limits={k:v for k,v in self.protection_limits.items() if k[1]!=ticket}
            self.protection_limits[(self.account_pin,ticket,sl,tp)] = max_age
            return {"action":self.m.TRADE_ACTION_SLTP,"position":ticket,"symbol":p["symbol"],"sl":sl,"tp":tp,"magic":magic}

    def prepare_close(self, ticket, risk):
        with self.lock:
            self._guard()
            pos = next((x for x in self.positions() if x["ticket"] == ticket), None)
            if not pos or pos["magic"] != risk["magic"]:
                raise BrokerError("只能平掉本框架管理的指定持仓")
            tick = self._need(self.m.symbol_info_tick(pos["symbol"]), "平仓报价")
            age = time.time() - (tick.time - self.offset)
            if age < -5 or age > risk["max_tick_age_seconds"]:
                raise BrokerError("平仓报价过期或时间偏移错误")
            side = "SELL" if pos["type"] == self.m.POSITION_TYPE_BUY else "BUY"
            request = self.make_request(pos["symbol"], side, pos["volume"], tick.bid if side == "SELL" else tick.ask, 0, 0, risk, "fusion exit")
            request["position"] = ticket
            check = self.check_order(request)
            if check["retcode"] != 0:
                raise BrokerError(f"平仓预检拒绝：{check.get('comment')}")
            return request

    def close_position(self, ticket, risk):
        with self.lock:
            return self.send_order(self.prepare_close(ticket, risk))

    def shutdown(self):
        with self.lock:
            if self.m:
                self.m.shutdown()
