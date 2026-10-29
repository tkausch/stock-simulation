"""Inventory simulation with SimPy.

- Stock consists of discrete items held in lots; every lot has one unit price.
- Replenishment arrives as one lot (same price for all items of the lot).
- Demand is taken from the cheapest lots first.
- Demand orders arrive as a Poisson process (exponential inter-arrival times);
  order size ~ Normal(mu, sigma), rounded up to the next integer (at least 1).
- Reorder policy (s, S) with s = S * (1 - empty_trigger): whenever the inventory position (on hand + on order)
  falls to or below s, an order of S - position items is placed. It arrives
  after a lead time.
- The mean price per item in stock is recorded after every change.
- Demand orders and replenishment orders (with their lot price) are logged too.
"""
import heapq
import math
import random
from dataclasses import dataclass, field

import simpy


@dataclass
class Params:
    sim_time: float = 800.0
    demand_rate: float = 2.0        # Poisson arrival rate of demand orders per time unit
    order_mean: float = 5.0         # mean order size
    order_std: float = 2.0          # std dev of order size
    S: int = 1600                 # order-up-to level = stock capacity
    empty_trigger: float = 0.25     # rebuy when the stock isd this fraction empty
    lead_time_mean: float = 5.0     # mean lead time (exponential); set lead_time_fixed to overrie
    lead_time_fixed: float | None = None
    initial_stock: int = 1400
    initial_price: float = 15.0
    price_mean: float = 10.0        # purchase price per lot ~ Normal(price_mean, price_std)
    price_std: float = 5.0
    seed: int | None = 42

    @property
    def s(self) -> float:
        """Reorder point: capacity minus the empty fraction that triggers a rebuy."""
        return self.S * (1 - self.empty_trigger)


@dataclass
class Inventory:
    env: simpy.Environment
    lots: list = field(default_factory=list)   # min-heap of [price, seq, qty]
    count: int = 0
    value: float = 0.0
    on_order: int = 0
    _seq: int = 0
    history: list = field(default_factory=list)  # (time, count, mean_price)
    demand_orders: list = field(default_factory=list)  # (time, size)
    replenishments: list = field(default_factory=list)  # (arrival time, qty, price)
    demanded: int = 0
    unmet: int = 0

    def mean_price(self) -> float:
        return self.value / self.count if self.count else float("nan")

    @property
    def position(self) -> int:
        return self.count + self.on_order

    def _record(self):
        self.history.append((self.env.now, self.count, self.mean_price()))

    def add_lot(self, qty: int, price: float):
        self._seq += 1  # tie-breaker keeps heap comparisons on (price, seq) only
        heapq.heappush(self.lots, [price, self._seq, qty])
        self.count += qty
        self.value += qty * price
        self._record()

    def remove(self, qty: int) -> int:
        """Take qty items, cheapest lots first. Returns the quantity actually served."""
        self.demanded += qty
        served = 0
        while served < qty and self.lots:
            lot = self.lots[0]
            take = min(qty - served, lot[2])
            lot[2] -= take
            served += take
            self.count -= take
            self.value -= take * lot[0]
            if lot[2] == 0:
                heapq.heappop(self.lots)
        self.unmet += qty - served  # lost sales
        if not self.lots:
            self.value = 0.0  # kill floating point drift
        self._record()
        return served


def replenishment(env, inv: Inventory, p: Params, qty: int):
    lead = p.lead_time_fixed if p.lead_time_fixed is not None else random.expovariate(1 / p.lead_time_mean)
    yield env.timeout(lead)
    price = max(0.01, random.gauss(p.price_mean, p.price_std))
    inv.on_order -= qty
    inv.replenishments.append((env.now, qty, price))
    inv.add_lot(qty, price)


def check_reorder(env, inv: Inventory, p: Params):
    if inv.position <= p.s:
        qty = p.S - inv.position
        inv.on_order += qty
        env.process(replenishment(env, inv, p, qty))


def demand_process(env, inv: Inventory, p: Params):
    while True:
        yield env.timeout(random.expovariate(p.demand_rate))
        size = max(1, math.ceil(random.gauss(p.order_mean, p.order_std)))
        inv.demand_orders.append((env.now, size))
        inv.remove(size)
        check_reorder(env, inv, p)


def run(p: Params) -> Inventory:
    if p.seed is not None:
        random.seed(p.seed)
    env = simpy.Environment()
    inv = Inventory(env)
    if p.initial_stock:
        inv.add_lot(p.initial_stock, p.initial_price)
    env.process(demand_process(env, inv, p))
    env.run(until=p.sim_time)
    return inv


if __name__ == "__main__":
    params = Params()
    inv = run(params)

    print(f"{'time':>8} {'stock':>6} {'mean price':>11}")
    step = max(1, len(inv.history) // 25)
    for t, c, m in inv.history[::step]:
        print(f"{t:8.2f} {c:6d} {m:11.3f}")

    prices = [m for _, _, m in inv.history if not math.isnan(m)]
    print(f"\nEnd stock: {inv.count}, on order: {inv.on_order}, mean price now: {inv.mean_price():.3f}")
    print(f"Demanded: {inv.demanded}, unmet (lost): {inv.unmet} "
          f"({100 * inv.unmet / max(1, inv.demanded):.1f}%)")
    print(f"Mean price over events: {sum(prices) / len(prices):.3f}")

    try:
        import matplotlib.pyplot as plt
        ts, cs, ms = zip(*inv.history)
        fig, (a1, a2, a3, a4) = plt.subplots(4, 1, sharex=True, figsize=(14, 10))
        a1.step(ts, cs, where="post")
        a1.axhline(params.s, ls="--", c="r", label="s")
        a1.axhline(params.S, ls="--", c="g", label="S")
        a1.set_ylabel("stock"); a1.legend()
        a2.step(ts, ms, where="post")
        a2.set_ylabel("mean price")
        dt, dq = zip(*inv.demand_orders)
        a3.vlines(dt, 0, dq, lw=1)
        a3.set_ylabel("demand order size")
        rt, rq, rp = zip(*inv.replenishments)
        a4.vlines(rt, 0, rp, lw=1, color="tab:orange")
        a4.scatter(rt, rp, s=[q / 2 for q in rq], color="tab:orange")
        a4.axhline(params.price_mean, ls=":", c="gray")
        a4.set_ylabel("price per replenishment\n(marker size = qty)"); a4.set_xlabel("time")
        plt.tight_layout()
        plt.savefig("inventory_sim.png", dpi=120)
        print("Plot saved to inventory_sim.png")
    except ImportError:
        pass
