"""README example: monthly top-K by 6-month return, cash when VNINDEX < its 200-day average.

    python -m research.examples.example_strategy
"""

from research.backtest import HOLD_RULES, run


class TopKMomentum:
    name = "topk_mom_example"
    rules = HOLD_RULES                      # hold until we sell (no stop/targets/time stop)

    def __init__(self, k=5, lookback=126, market_ma=200, every=21):
        self.k, self.lookback, self.market_ma, self.every = k, lookback, market_ma, every

    def on_session(self, ctx):
        vni = ctx.vnindex                               # closes up to today only
        if len(vni) < self.market_ma or vni.iloc[-1] < vni.iloc[-self.market_ma:].mean():
            return {}                                   # empty basket -> sell everything next open
        n = ctx.state.get("n", 0)
        ctx.state["n"] = n + 1
        if n % self.every and ctx.positions:            # rebalance every `every` sessions
            return None                                 # None -> no new orders
        syms = ctx.universe()                           # point-in-time liq_topN for the next session
        px = ctx.history("close", syms, self.lookback + 1)
        ret = (px.iloc[-1] / px.iloc[0] - 1).dropna()   # NaN (no bar today / too new) dropped
        top = ret[ret > 0].nlargest(self.k).index
        return dict.fromkeys(top, 0.98 / self.k)       # weights -> orders at the next open


if __name__ == "__main__":
    res = run(TopKMomentum(), "2013-01-01", "2020-12-31", universe_kind="liq_top50")
    print(res.summary())
    print(res.yearly)
