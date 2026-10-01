"""Offline PID exit-envelope experiment. No broker, order, AI or runtime integration.

The controller moves a software centre, not the market. Local bounds may move in
both directions; only the simulated broker stop has a tightening-only constraint.
"""
from dataclasses import asdict, dataclass
import math


def clamp(value, lower, upper):
    return max(lower, min(upper, value))


@dataclass(frozen=True)
class EnvelopeConfig:
    side: int = 1
    entry: float = 2500.0
    units: float = 1.0  # Synthetic USD PnL per one price unit; not a broker contract.
    costs: float = 0.20  # Fixed estimated round-trip cost; spread is already in quote.
    activation: float = 3.0
    lock_profit: float = 3.0
    upper_distance: float = 1.0
    lower_distance: float = 1.0
    kp: float = 0.8       # 1 / second
    ki: float = 0.04      # 1 / second squared
    kd: float = 0.12      # dimensionless, derivative acts on tracking error
    filter_seconds: float = 0.35
    max_speed: float = 1.5  # price units / second
    integral_limit: float = 20.0
    max_dt: float = 0.25   # longer gaps reset memory, never extrapolate missing prices
    price_step: float = 0.01
    broker_distance: float = 0.35
    ack_delay: float = 0.4
    close_delay: float = 0.25
    slippage: float = 0.05
    initial_risk: float = 6.0

    def __post_init__(self):
        if self.side not in (-1, 1):
            raise ValueError("side must be +1 or -1")
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"non-finite/invalid {name}")
            if name != "side" and value < 0:
                raise ValueError(f"negative {name}")
        for name in ("entry", "units", "activation", "upper_distance", "lower_distance", "filter_seconds", "max_speed", "integral_limit", "max_dt", "price_step", "initial_risk"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


class PIDCentre:
    def __init__(self, value, config):
        self.value = value
        self.config = config
        self.integral = self.derivative = self.previous_error = 0.0

    def update(self, quote, dt):
        c = self.config
        if dt <= 0:
            return self.value  # same-millisecond quotes still undergo crossing checks
        error = quote - self.value
        if dt > c.max_dt:
            self.integral = self.derivative = 0.0
            self.previous_error = error
        dt = min(dt, c.max_dt)
        alpha = dt / (c.filter_seconds + dt)
        self.derivative += alpha * ((error - self.previous_error) / dt - self.derivative)
        candidate = clamp(self.integral + error * dt, -c.integral_limit, c.integral_limit) if c.ki else 0.0

        def output(integral):
            raw = c.kp * error + c.ki * integral + c.kd * self.derivative
            move = clamp(raw, -c.max_speed, c.max_speed) * dt
            # Software actuator must not overshoot or move away from its target.
            move = clamp(move, min(0.0, error), max(0.0, error))
            return raw, move

        raw, move = output(candidate)
        if (raw - move / dt) * error > 1e-12:
            raw, move = output(self.integral)  # conditional integration / anti-windup
        else:
            self.integral = candidate
        self.value += move
        self.previous_error = error
        return self.value


class EnvelopeExperiment:
    """Causal synthetic execution model; acknowledgments/fills are simulated only."""
    def __init__(self, config=None):
        self.config = config or EnvelopeConfig()
        self.pid = None
        self.last_time = None
        self.last_pid_time = None
        self.broker_sl = self.price_for_profit(-self.config.initial_risk)
        self.pending_stop = None
        self.exit_request = None
        self.fill = None
        self.events = []
        self.activated_at = None
        self.lock_confirmed = False

    def profit(self, quote):
        c = self.config
        return c.side * (quote - c.entry) * c.units - c.costs

    def price_for_profit(self, amount):
        c = self.config
        return c.entry + c.side * (amount + c.costs) / c.units

    def lock_price(self):
        c = self.config
        raw = self.price_for_profit(c.lock_profit) / c.price_step
        return (math.ceil(raw - 1e-9) if c.side == 1 else math.floor(raw + 1e-9)) * c.price_step

    def bounds(self):
        if self.pid is None:
            return None, None
        return self.pid.value - self.config.lower_distance, self.pid.value + self.config.upper_distance

    def step(self, t, quote, online=True):
        if not math.isfinite(t) or not math.isfinite(quote) or t < 0 or quote <= 0:
            raise ValueError("invalid tick")
        if self.last_time is not None and t < self.last_time:
            raise ValueError("out-of-order tick")
        c = self.config
        dt = 0 if self.last_time is None else t - self.last_time
        self.last_time = t
        emitted = []

        def event(kind, **data):
            value = {"kind": kind, "time": t, "price": quote, **data}
            self.events.append(value)
            emitted.append(value)

        # The confirmed server stop remains active when the local terminal is offline.
        if self.fill is None:
            server_hit = c.side * (quote - self.broker_sl) <= 0
            if server_hit and (self.exit_request is None or self.exit_request["source"] != "broker"):
                event("broker_touch", level=self.broker_sl)
                # Server-side stop supersedes a pending local exit in this simplified model.
                self.exit_request = {"source": "broker", "time": t, "due": t + c.close_delay, "level": self.broker_sl}

            lower, upper = self.bounds()
            # IMPORTANT: test the OLD active envelope before moving either boundary.
            if online and self.exit_request is None and lower is not None:
                crossed = "lower" if quote <= lower else "upper" if quote >= upper else None
                if crossed:
                    level = lower if crossed == "lower" else upper
                    event("local_touch", boundary=crossed, level=level)
                    self.exit_request = {"source": "local", "boundary": crossed, "time": t, "due": t + c.close_delay, "level": level}

            if self.exit_request is not None:
                if t >= self.exit_request["due"] and (online or self.exit_request["source"] == "broker"):
                    actual = quote - c.side * c.slippage
                    self.fill = {"time": t, "price": actual, "profit": self.profit(actual), "source": self.exit_request["source"]}
                    event("fill", execution_price=actual, profit=self.fill["profit"], source=self.fill["source"])
            elif online:
                if self.pid is None and self.profit(quote) >= c.activation - 1e-9:
                    self.pid = PIDCentre(quote, c)
                    self.last_pid_time = t
                    self.activated_at = t
                    event("activated")
                elif self.pid is not None:
                    self.pid.update(quote, t - self.last_pid_time)
                    self.last_pid_time = t

                # This intentionally models a pending response separately from a valid stop.
                target = self.lock_price()
                valid = c.side * (quote - target) >= c.broker_distance + c.price_step
                tighter = c.side * (target - self.broker_sl) > 0
                if self.pending_stop and t >= self.pending_stop["due"]:
                    if valid and tighter:
                        self.broker_sl = self.pending_stop["price"]
                        self.lock_confirmed = True
                        event("stop_ack", level=self.broker_sl)
                    else:
                        event("stop_rejected")
                    self.pending_stop = None
                if self.pid is not None and not self.lock_confirmed and self.pending_stop is None and valid and tighter:
                    self.pending_stop = {"price": target, "due": t + c.ack_delay}
                    event("stop_request", level=target)

        lower, upper = self.bounds()
        state = "closed" if self.fill else "exit_pending" if self.exit_request else "tracking" if self.pid else "waiting"
        return {"time": t, "price": quote, "online": online, "profit": self.profit(quote),
                "lower": lower, "upper": upper, "centre": self.pid.value if self.pid else None,
                "broker_sl": self.broker_sl, "lock_confirmed": self.lock_confirmed,
                "stop_pending": self.pending_stop is not None, "state": state, "events": emitted,
                "fill": self.fill, "activation_time": self.activated_at}


def synthetic_ticks(scenario="rise", side=1):
    """Deterministic irregular ticks, synthetic one-unit exposure, never live data."""
    if scenario not in {"normal", "rise", "fall", "gap", "offline"}:
        raise ValueError("unknown scenario")
    t = 0.0
    for index in range(1100):
        t += (0.08, 0.12, 0.16, 0.10)[index % 4]
        gain = 0.085 * t - 0.20 + 0.055 * math.sin(t * 1.7) + 0.035 * math.sin(t * 4.3)
        shock = 0.0
        if t >= 80:
            shock = {"normal": 0, "rise": 2.6, "fall": -1.9, "gap": -6.0, "offline": -4.2}[scenario]
        quote = 2500 + side * gain + shock
        yield {"time": round(t, 6), "price": quote, "online": not (scenario == "offline" and 70 <= t < 95)}


def simulate(config=None, scenario="rise"):
    engine = EnvelopeExperiment(config)
    rows = [engine.step(tick["time"], tick["price"], tick["online"]) for tick in synthetic_ticks(scenario, engine.config.side)]
    return {"config": asdict(engine.config), "scenario": scenario, "rows": rows, "events": engine.events, "fill": engine.fill}
