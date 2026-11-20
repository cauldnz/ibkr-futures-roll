"""Offline tests for roll_futures.py.

Runs the whole CLI against a fake ib_async (no TWS needed), using real quotes captured on 2026-11-20.
Run: python tests/test_roll.py
"""
import io
import json
import os
import tempfile
from pathlib import Path
import math
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from datetime import date

NAN = float("nan")


# ---------------- fake ib_async
class Contract:
    def __init__(self, **kw):
        self.conId = 0; self.symbol = ""; self.secType = ""; self.exchange = ""; self.currency = ""
        self.localSymbol = ""; self.tradingClass = ""; self.multiplier = ""; self.lastTradeDateOrContractMonth = ""
        self.comboLegs = []
        self.__dict__.update(kw)


class ComboLeg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class LimitOrder:
    def __init__(self, action, qty, price, **kw):
        self.action, self.totalQuantity, self.lmtPrice = action, qty, price
        self.orderRef = ""
        self.__dict__.update(kw)


class CD:
    def __init__(self, c, minTick, month):
        self.contract, self.minTick, self.contractMonth = c, minTick, month


class T:
    def __init__(self, bid, ask, bs=10, as_=10):
        self.bid, self.ask, self.bidSize, self.askSize = bid, ask, bs, as_


def fut(conId, sym, exch, cur, local, ltd, mult, tclass=None):
    return Contract(conId=conId, symbol=sym, secType="FUT", exchange=exch, currency=cur, localSymbol=local,
                    lastTradeDateOrContractMonth=ltd, multiplier=mult, tradingClass=tclass or sym)


C = {c.conId: c for c in [
    fut(1, "ZN", "CBOT", "USD", "ZNZ6", "20261221", "1000"),
    fut(2, "ZN", "CBOT", "USD", "ZNH7", "20270319", "1000"),
    fut(3, "ZT", "CBOT", "USD", "ZTZ6", "20261231", "2000"),
    fut(4, "ZT", "CBOT", "USD", "ZTH7", "20270331", "2000"),
    fut(5, "UB", "CBOT", "USD", "UBZ6", "20261221", "1000"),
    fut(6, "UB", "CBOT", "USD", "UBH7", "20270319", "1000"),
    fut(7, "MGC", "COMEX", "USD", "MGCZ6", "20261229", "10"),
    fut(8, "MGC", "COMEX", "USD", "MGCF7", "20270127", "10"),
    fut(9, "MGC", "COMEX", "USD", "MGCG7", "20270224", "10"),
    fut(10, "XINA50", "SGX", "USD", "CNX26", "20261127", "1"),
    fut(11, "XINA50", "SGX", "USD", "CNZ26", "20261230", "1"),
    fut(12, "SPI", "SNFE", "AUD", "APZ6", "20261217", "25", "AP"),
    fut(13, "SPI", "SNFE", "AUD", "APH7", "20270318", "25", "AP"),
    fut(14, "MCL", "NYMEX", "USD", "MCLF7", "20261218", "100"),
    fut(15, "MCL", "NYMEX", "USD", "MCLG7", "20270119", "100"),
    fut(16, "MES", "CME", "USD", "MESZ6", "20261218", "5"),
    fut(17, "MES", "CME", "USD", "MESH7", "20270319", "5"),
]}
TICK = {"ZN": 1 / 64, "ZT": 1 / 256, "UB": 1 / 32, "MGC": 0.1, "XINA50": 1.0, "SPI": 1.0, "MCL": 0.01, "MES": 0.25}
POS = {1: 4, 2: 1, 3: 6, 5: -2, 7: 2, 10: 5, 12: -1, 14: 2, 15: 1, 16: -3}   # made-up sizes
Q = {1: (106.03125, 106.046875), 2: (105.890625, 105.90625), 3: (101.99609375, 102.0),
     4: (101.8828125, 101.890625), 5: (107.96875, 108.0), 6: (107.65625, 107.6875),
     7: (3602.4, 3602.6), 9: (3631.1, 3631.4), 10: (12498.0, 12500.0), 11: (12390.0, 12394.0),
     12: (NAN, NAN), 13: (NAN, NAN), 16: (7174.0, 7174.5), 17: (7243.0, 7244.5)}
# spread book (next - current), keyed by (next, current); quoted to 6 dp like TWS
BOOK = {(2, 1): (-0.144531, -0.140625), (4, 3): (-0.113281, -0.109375), (6, 5): (-0.335938, -0.328125),
        (9, 7): (28.7, 28.8), (11, 10): (NAN, NAN), (13, 12): (-12, -7), (17, 16): (69.2, 69.4)}
GRID_OK = {"ZN": 1 / 128, "ZT": 1 / 256, "UB": 1 / 128, "MGC": 0.1, "XINA50": 1.0, "SPI": 1.0, "MES": 0.05}


class Event:
    def __init__(self):
        self.h = []

    def __iadd__(self, f):
        self.h.append(f)
        return self

    def emit(self, *a):
        for f in self.h:
            f(*a)


class Pos:
    def __init__(self, c, q):
        self.contract, self.position = c, q


class OS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class IB:
    placed = []

    def __init__(self):
        self.errorEvent = Event()

    def connect(self, *a, **k): pass
    def reqMarketDataType(self, t): pass
    def managedAccounts(self): return ["U0000000"]
    def positions(self, account=None): return [Pos(C[i], q) for i, q in POS.items()]
    def reqAllOpenOrders(self): pass
    def sleep(self, s): pass
    def openTrades(self): return []
    def reqCompletedOrders(self, apiOnly): return []
    def qualifyContracts(self, *cs): return cs
    def disconnect(self): pass
    def cancelMktData(self, c): pass

    def reqContractDetails(self, c):
        if c.conId:
            x = C[c.conId]
            return [CD(x, TICK[x.symbol], x.lastTradeDateOrContractMonth[:6])]
        return [CD(x, TICK[x.symbol], x.lastTradeDateOrContractMonth[:6]) for x in C.values()
                if x.symbol == c.symbol and x.exchange == c.exchange]

    def reqMktData(self, c, *a):
        if c.secType == "BAG":
            nxt, cur = c.comboLegs[0].conId, c.comboLegs[1].conId
            b, k = BOOK.get((nxt, cur), (NAN, NAN))
            return T(b, k)
        b, k = Q.get(c.conId, (NAN, NAN))
        return T(b, k)

    def whatIfOrder(self, bag, o):
        g = GRID_OK[bag.symbol]
        if abs(round(o.lmtPrice / g) * g - o.lmtPrice) > 1e-9:
            self.errorEvent.emit(1, 110, "The price does not conform to the minimum price variation", bag)
            return []
        return OS(initMarginChange="100", maintMarginChange="50", commission=1.0, commissionCurrency="USD")

    def placeOrder(self, bag, o):
        IB.placed.append((bag.symbol, o.action, o.totalQuantity, o.lmtPrice))
        return types.SimpleNamespace(orderStatus=types.SimpleNamespace(status="Submitted"), log=[])


fake = types.ModuleType("ib_async")
fake.IB, fake.ComboLeg, fake.Contract, fake.LimitOrder = IB, ComboLeg, Contract, LimitOrder
fake.util = types.SimpleNamespace(isNan=lambda x: isinstance(x, float) and math.isnan(x))
sys.modules["ib_async"] = fake
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.chdir(TMP)   # so no stray roll.toml is picked up
import roll_futures as rf  # noqa: E402


class FixedDate(date):
    @classmethod
    def today(cls):
        return date(2026, 11, 20)


rf.date = FixedDate


STDIN_LINE = "ROLL\n"


def run(*argv, stdin_tty=True):
    sys.argv = ["roll_futures.py", *argv]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    sys.stdin = types.SimpleNamespace(isatty=lambda: stdin_tty, readline=lambda: STDIN_LINE)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            rf.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    return code, out.getvalue(), err.getvalue()


fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- pure helpers
check("ZN snaps -4.625/32 down to -4.75/32",
      abs(rf.passive_price("BUY", -0.144531, -0.140625, NAN, NAN, 1 / 128, 1 / 256)[0] * 32 + 4.75) < 1e-9)
check("ZT joins -3.625/32", abs(rf.passive_price("BUY", -0.113281, NAN, NAN, NAN, 1 / 256, 1 / 256)[0] * 32 + 3.625) < 1e-9)
check("UB sell joins -10.5/32", abs(rf.passive_price("SELL", NAN, -0.328125, NAN, NAN, 1 / 128, 1 / 256)[0] * 32 + 10.5) < 1e-9)
check("MES sell joins 69.4 on 0.05 grid", abs(rf.passive_price("SELL", 69.2, 69.4, NAN, NAN, 0.05)[0] - 69.4) < 1e-9)
check("MES sell on 0.25 grid snaps UP to 69.5", abs(rf.passive_price("SELL", 69.2, 69.4, NAN, NAN, 0.25)[0] - 69.5) < 1e-9)
check("implied fallback", rf.passive_price("BUY", NAN, NAN, -110, -104, 1.0) == (-110, "joins leg-implied bid"))
check("ticks away moves back", rf.passive_price("BUY", 28.7, 28.8, NAN, NAN, 0.1, None, 2)[0] == 28.5)
check("no quote", rf.passive_price("BUY", NAN, NAN, NAN, NAN, 1.0)[0] is None)
check("crosses detects", rf.crosses("BUY", 28.8, 28.7, 28.8) and not rf.crosses("BUY", 28.7, 28.7, 28.8))
cds = [CD(C[i], 0.1, C[i].lastTradeDateOrContractMonth[:6]) for i in (7, 8, 9)]
check("next listed expiry is Jan", rf.choose_back(cds[0], cds).contract.localSymbol == "MGCF7")
check("back_month picks Feb", rf.choose_back(cds[0], cds, "202702").contract.localSymbol == "MGCG7")

# ---- end to end, no config, 45-day window
code, out, err = run("--within", "45", "--list")
check("list: ZN rolls Dec not Mar", "ZN: ZNZ6 +4" in out and "ZNH7 +1" in out)
check("list: MCL nearest only", "MCL: MCLF7 +2" in out)

code, out, err = run("--within", "45", "--json")
res = json.loads(out)
r = {x["symbol"]: x for x in res["rolls"]}
check("json parses and NaN -> null", r["XINA50"]["book_bid"] is None)
check("ZN BUY at -4.75/32", r["ZN"]["action"] == "BUY" and abs(r["ZN"]["limit"] * 32 + 4.75) < 1e-9 and r["ZN"]["staged"])
check("UB SELL at -10.5/32", r["UB"]["action"] == "SELL" and abs(r["UB"]["limit"] * 32 + 10.5) < 1e-9)
check("MGC default next = Jan", r["MGC"]["next"] == "MGCF7")
check("XINA50 implied -110", r["XINA50"]["limit"] == -110)
check("SPI sells at -7", r["SPI"]["action"] == "SELL" and r["SPI"]["limit"] == -7)
check("MES sell joins 69.4", abs(r["MES"]["limit"] - 69.4) < 1e-9 and r["MES"]["staged"])
check("dry run exit 0", code == 0)

# ---- with a typical config
CFG = TMP / "test.toml"
CFG.open("w").write('''
pairs = [["ZT", "UB"]]
[defaults]
within_days = 45
[products.MGC]
back_month = "202702"
[products.SPI]
skip = true
note = "hand-rolled"
[products.MCL]
skip = true
''')
code, out, err = run("--config", str(CFG), "--json")
res = json.loads(out)
r = {x["symbol"]: x for x in res["rolls"]}
check("config: MGC -> Feb", r["MGC"]["next"] == "MGCG7")
check("config: SPI, MCL skipped", "SPI" not in r and "MCL" not in r and "hand-rolled" in err)

code, out, err = run("--config", str(CFG), "--only", "ZT", "--json")
check("pair warning when only ZT", "configured as a pair" in err)

# ---- live safety
code, out, err = run("--config", str(CFG), "--live", "--json", stdin_tty=False)
check("live without tty and without --yes refuses (exit 3)", code == 3 and not IB.placed)
code, out, err = run("--config", str(CFG), "--only", "MGC", "--live", "--yes", "--json")
check("live --yes places MGC buy 2 @ 28.7", IB.placed == [("MGC", "BUY", 2, 28.7)] and code == 0)

# ---- security hardening
IB.placed.clear()
code, out, err = run("--config", str(CFG), "--only", "MGC", "--limit", "MGC=28.8", "--json")
r = {x["symbol"]: x for x in json.loads(out)["rolls"]}
check("manual limit that crosses is refused", r["MGC"]["crosses"] and not r["MGC"]["staged"] and code == 1)
code, out, err = run("--config", str(CFG), "--only", "MGC", "--limit", "MGC=28.8", "--live", "--yes", "--json")
check("crossing order never placed even with --live --yes", IB.placed == [] and code == 1)
code, out, err = run("--ticks-away", "-2")
check("negative --ticks-away rejected", code == 2 and "must be 0 or more" in err)
code, out, err = run("--limit", "MGC")
check("malformed --limit rejected", code == 2)
code, out, err = run("--yes")
check("--yes without --live rejected", code == 2)
STDIN_LINE = "no\n"
code, out, err = run("--config", str(CFG), "--only", "MGC", "--live", "--json")
check("typing anything but ROLL aborts (exit 3), nothing placed", code == 3 and IB.placed == [])
check("prompt goes to stderr, stdout stays valid JSON", "Type ROLL" in err and json.loads(out)["mode"] == "live")
STDIN_LINE = "ROLL\n"
orig = IB.whatIfOrder
IB.whatIfOrder = lambda self, bag, o: (_ for _ in ()).throw(RuntimeError("boom"))
code, out, err = run("--config", str(CFG), "--only", "MGC", "--live", "--yes", "--json")
check("what-if exception -> not staged, nothing placed", IB.placed == [] and code == 1)
IB.whatIfOrder = orig
code, out, err = run("--within", "45", "--only", "MGC", "--limit", "MGC=28.8")
check("manual limit with no quotes warns it cannot be checked", "cannot check" in err + out)

# ---- status
code, out, err = run("--status", "--json")
check("status with no orders exits 1", code == 1 and json.loads(out)["orders"] == [])

print("\nFAILURES:", fails if fails else "none")
sys.exit(1 if fails else 0)
