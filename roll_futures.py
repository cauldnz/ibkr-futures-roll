#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["ib_async>=1.0,<3"]
# ///
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Chris Auld
"""
roll_futures.py - roll Interactive Brokers futures positions as PASSIVE calendar-spread orders via TWS.

What it does
  1. Reads your futures positions from TWS / IB Gateway.
  2. Picks the ones expiring within --within days (default 35) and finds the next listed expiry
     of the same product (or the month you set in a config file).
  3. Builds one calendar-spread (BAG) order per position and prices it on the NON-MARKETABLE side
     of the spread book (join the bid when buying, the ask when selling), so it rests rather than
     fills. You then walk the price yourself in TWS or IBKR Mobile.
  4. Dry run by default. --live places the orders after you type ROLL.

Direction is fixed so it cannot be inverted:
  every spread is defined as  BUY next month / SELL current month  (spread price = next - current)
  long  position -> BUY  the spread  (sells current, buys next)
  short position -> SELL the spread  (buys current, sells next)
IBKR describes these as "Buy/Sell ... Reverse Calendar". The dry run prints the actual leg trades and
the position after the roll before anything is sent.

Run:  uv run roll_futures.py [--list | --status | (dry run) | --live] [--json]
  (or: pip install ib_async && python roll_futures.py ...)
Exit codes: 0 ok, 1 nothing to do / nothing staged, 2 connection or config error, 3 aborted.

Requirements: Python 3.11+, ib_async, TWS or IB Gateway with the API enabled
(see README). No warranty: always run the dry run first and check every line.
"""
import argparse
import json
import math
import sys
import tomllib
from datetime import date, datetime
from pathlib import Path

from ib_async import IB, ComboLeg, Contract, LimitOrder, util

# ---------------------------------------------------------------------------------------------
# Calendar-spread price grids where they are FINER than the outright tick. The script only joins
# a price that is already on the spread book, and snaps anything off-grid AWAY from the market, so
# an entry that is too coarse just rests a little further back; one that is too fine gets the
# order rejected by TWS (error 110) in the dry run's what-if check, never filled.
# Override or extend per product in the config file (spread_tick = ...).
SPREAD_GRID = {
    "ZN": 1 / 128,   # CBOT 10y: quarter of a 32nd (confirmed: 1/256 rejected, 1/128 accepted)
    "ZT": 1 / 256,   # CBOT 2y: eighth of a 32nd (confirmed accepted)
    "UB": 1 / 128,   # CBOT Ultra Bond: quarter 32nd (half-32nd prices accepted; finer not tested)
    "MES": 0.05, "MNQ": 0.05, "M2K": 0.05,   # CME equity-index calendars: inferred from book quotes
}
# Products whose prices read better in 32nds.
THIRTY_SECONDS = {"ZT", "ZF", "ZN", "TN", "TWE", "ZB", "UB"}
# TWS sends combo quotes rounded to ~6 decimals; clean these products to 1/256 before snapping.
QUOTE_CLEAN = {k: 1 / 256 for k in THIRTY_SECONDS}


# ------------------------------------------------------------------------------- pure helpers
def bad(x):
    return x is None or (isinstance(x, float) and math.isnan(x)) or x == -1


def to_tick(p, tick, down):
    """Snap p onto a grid of `tick`, rounding down (down=True) or up."""
    n = p / tick
    n = math.floor(n + 1e-9) if down else math.ceil(n - 1e-9)
    return round(n * tick, 10)


def passive_price(action, book_bid, book_ask, impl_bid, impl_ask, grid, clean=None, ticks_away=0):
    """Price that rests on the passive side. Returns (price, source) or (None, reason)."""
    buy = action == "BUY"
    book = book_bid if buy else book_ask
    impl = impl_bid if buy else impl_ask
    side = "bid" if buy else "ask"
    if not bad(book):
        ref, src = book, f"spread book {side}"
    elif not bad(impl):
        ref, src = impl, f"leg-implied {side}"
    else:
        return None, "no usable quote"
    if clean:
        ref = round(ref / clean) * clean
    ref += (-1 if buy else 1) * ticks_away * grid
    price = to_tick(ref, grid, down=buy)            # never moves toward the other side
    if ticks_away:
        src += f", {ticks_away} tick(s) further back"
    elif abs(price - ref) > 1e-9:
        src += f" {ref:.6g} snapped passive to grid {grid:g}"
    else:
        src = "joins " + src
    return price, src


def crosses(action, price, book_bid, book_ask):
    if action == "BUY":
        return not bad(book_ask) and price >= book_ask - 1e-12
    return not bad(book_bid) and price <= book_bid + 1e-12


def expiry(cd):
    """Last trade date of a ContractDetails / Contract as a date."""
    s = getattr(cd, "contract", cd).lastTradeDateOrContractMonth[:8]
    return datetime.strptime(s if len(s) == 8 else s + "01", "%Y%m%d").date()


def choose_back(front_cd, listed, month=None):
    """Next listed expiry after the front, or the first listed contract in `month` (YYYYMM)."""
    later = sorted((cd for cd in listed if expiry(cd) > expiry(front_cd)), key=expiry)
    if month:
        later = [cd for cd in later if cd.contractMonth == month
                 or cd.contract.lastTradeDateOrContractMonth.startswith(month)]
    return later[0] if later else None


def fmt(symbol, p):
    if bad(p):
        return "n/a"
    return f"{p:.6g} ({p * 32:+.3f}/32)" if symbol in THIRTY_SECONDS else f"{p:.6g}"


def jsonable(x):
    """NaN is not valid JSON: turn it into null, recursively."""
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def load_config(path):
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        sys.exit(f"config file not found: {p}")
    with p.open("rb") as fh:
        return tomllib.load(fh)


# ------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Roll IBKR futures as passive calendar spreads (dry run by default).")
    ap.add_argument("--config", default="roll.toml" if Path("roll.toml").exists() else None,
                    help="TOML config (default: ./roll.toml if present)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=None, help="7496 TWS live, 7497 TWS paper (default 7496)")
    ap.add_argument("--client-id", type=int, default=17)
    ap.add_argument("--account", default=None, help="default: first managed account")
    ap.add_argument("--only", nargs="*", default=None, help="symbols to consider, e.g. ZN ZT UB")
    ap.add_argument("--within", type=int, default=None, help="roll positions expiring within N days (default 35)")
    ap.add_argument("--list", action="store_true", help="list held futures and their roll targets, then stop")
    ap.add_argument("--status", action="store_true", help="show working ROLL orders and fills, then stop")
    ap.add_argument("--live", action="store_true", help="actually place orders (asks you to type ROLL)")
    ap.add_argument("--yes", action="store_true",
                    help="with --live, skip the typed ROLL confirmation. Only for a person who has just "
                         "reviewed the dry run; an agent must not pass this on its own initiative")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable JSON on stdout; human-readable log goes to stderr")
    ap.add_argument("--ticks-away", type=int, default=0, help="rest N spread ticks further from the touch")
    ap.add_argument("--tif", default="DAY", choices=["DAY", "GTC"])
    ap.add_argument("--mdtype", type=int, default=1, help="market data type: 1 live, 2 frozen, 3 delayed")
    ap.add_argument("--wait", type=float, default=4.0, help="seconds to collect quotes")
    ap.add_argument("--snapshot", action="store_true",
                    help="request one-off snapshots where streaming quotes are missing (IBKR charges per snapshot)")
    ap.add_argument("--limit", nargs="*", default=[], metavar="SYMBOL=PRICE",
                    help="manual spread limit (next minus current), overrides the passive calc")
    a = ap.parse_args()
    if a.ticks_away < 0:
        ap.error("--ticks-away must be 0 or more (it only moves the price further from the market)")
    for kv in a.limit:
        k, _, v = kv.partition("=")
        try:
            float(v)
        except ValueError:
            ap.error(f"--limit expects SYMBOL=PRICE, got {kv!r}")
        if not k:
            ap.error(f"--limit expects SYMBOL=PRICE, got {kv!r}")
    if a.yes and not a.live:
        ap.error("--yes only applies with --live")

    out = sys.stderr if a.json else sys.stdout

    def say(*args, **kw):
        print(*args, file=out, **kw)

    result = {"mode": "list" if a.list else "status" if a.status else "live" if a.live else "dry_run",
              "rolls": [], "placed": [], "errors": []}

    def finish(code):
        if a.json:
            print(json.dumps(jsonable(result), indent=2, default=str))
        sys.exit(code)

    cfg = load_config(a.config)
    if a.config:
        say(f"config: {Path(a.config).resolve()}")
    dflt = cfg.get("defaults", {})
    prod = {k.upper(): v for k, v in cfg.get("products", {}).items()}
    pairs = [[s.upper() for s in g] for g in cfg.get("pairs", [])]
    port = a.port or dflt.get("port", 7496)
    within = a.within if a.within is not None else dflt.get("within_days", 35)
    only = {s.upper() for s in a.only} if a.only else None
    manual = {k.upper(): float(v) for k, _, v in (kv.partition("=") for kv in a.limit)}
    today = date.today()

    ib = IB()
    errors = result["errors"]
    ib.errorEvent += lambda reqId, code, msg, contract: errors.append(
        {"reqId": reqId, "code": code, "message": msg,
         "contract": getattr(contract, "localSymbol", None) or getattr(contract, "symbol", None)})
    try:
        ib.connect(a.host, port, clientId=a.client_id)
    except Exception as e:  # noqa: BLE001
        say(f"could not connect to TWS/Gateway at {a.host}:{port}: {e!r}. Is the API enabled? (see README)")
        result["errors"].append({"code": "connect", "message": repr(e)})
        finish(2)
    ib.reqMarketDataType(a.mdtype)
    account = a.account or dflt.get("account") or ib.managedAccounts()[0]
    result["account"] = account
    say(f"account: {account}   window: {within} days   {'LIVE' if a.live else 'dry run'}")

    held = {}
    for p in ib.positions(account):
        if p.contract.secType == "FUT" and p.position:
            held[p.contract.conId] = (p.contract, p.position)
    ib.reqAllOpenOrders()
    ib.sleep(1)
    working_refs = {t.order.orderRef for t in ib.openTrades() if t.order.orderRef}

    if a.status:
        seen, rows = set(), []
        for t in list(ib.openTrades()) + list(ib.reqCompletedOrders(False)):
            if id(t) in seen:
                continue
            seen.add(id(t))
            ref = t.order.orderRef or ""
            if not ref.startswith("ROLL "):
                continue
            rows.append(dict(ref=ref, action=t.order.action, qty=t.order.totalQuantity,
                             limit=t.order.lmtPrice, status=t.orderStatus.status,
                             filled=t.orderStatus.filled, remaining=t.orderStatus.remaining,
                             avg_fill=t.orderStatus.avgFillPrice))
        for r in rows:
            say(f"{r['ref']:<34} {r['action']:<4} {r['qty']:>5g} @ {r['limit']:<12g} {r['status']:<12} "
                f"filled {r['filled']:g}/{r['qty']:g}" + (f" avg {r['avg_fill']:g}" if r['filled'] else ""))
        if not rows:
            say("no ROLL orders found today")
        result["orders"] = rows
        ib.disconnect()
        finish(0 if rows else 1)

    # ---- pick what to roll: nearest held expiry per product, inside the window, not skipped
    by_product = {}
    for c, qty in held.values():
        ib.qualifyContracts(c)
        key = (c.symbol, c.exchange, c.tradingClass)
        by_product.setdefault(key, []).append((c, qty))

    plans = []
    for (sym, exch, tclass), rows in sorted(by_product.items()):
        rows.sort(key=lambda r: expiry(r[0]))
        c, qty = rows[0]
        days = (expiry(c) - today).days
        pc = prod.get(sym, {})
        if only and sym not in only:
            continue
        if pc.get("skip"):
            say(f"{sym} {c.localSymbol} {qty:+g}: skipped by config ({pc.get('note', 'skip = true')})")
            continue
        win = int(pc.get("within_days", within)) if a.within is None else within
        if days > win:
            if a.list:
                say(f"{sym} {c.localSymbol} {qty:+g}: {days} days to last trade - outside the {win}-day window")
            continue
        fcd = ib.reqContractDetails(Contract(conId=c.conId))[0]
        listed = ib.reqContractDetails(Contract(secType="FUT", symbol=sym, exchange=exch, currency=c.currency,
                                                tradingClass=tclass, multiplier=c.multiplier))
        bcd = choose_back(fcd, listed, pc.get("back_month"))
        if not bcd:
            say(f"{sym} {c.localSymbol}: no later expiry found{' in ' + pc['back_month'] if pc.get('back_month') else ''}, skipping")
            continue
        if len(rows) > 1:
            others = ", ".join(f"{r[0].localSymbol} {r[1]:+g}" for r in rows[1:])
            say(f"{sym}: also holding {others} (only the nearest, {c.localSymbol}, is rolled)")
        plans.append(dict(sym=sym, fcd=fcd, bcd=bcd, fc=fcd.contract, bc=bcd.contract, qty=qty, days=days,
                          pc=pc, note=pc.get("note")))

    if a.list:
        for p in plans:
            result["rolls"].append(dict(symbol=p["sym"], current=p["fc"].localSymbol, next=p["bc"].localSymbol,
                                        qty=p["qty"], days=p["days"], note=p["note"]))
            say(f"{p['sym']}: {p['fc'].localSymbol} {p['qty']:+g}, {p['days']} days to last trade "
                  f"-> {p['bc'].localSymbol} ({p['bc'].lastTradeDateOrContractMonth})"
                  + (f"   [{p['note']}]" if p['note'] else ""))
        ib.disconnect()
        finish(0 if plans else 1)

    chosen = {p["sym"] for p in plans}
    for g in pairs:
        part = [s for s in g if s in chosen]
        if part and len(part) != len(g):
            say(f"!! WARNING: {'/'.join(g)} are configured as a pair; only {', '.join(part)} would be rolled")

    # ---- quotes
    for p in plans:
        fc, bc = p["fc"], p["bc"]
        bag = Contract(symbol=fc.symbol, secType="BAG", currency=fc.currency, exchange=fc.exchange)
        bag.comboLegs = [ComboLeg(conId=bc.conId, ratio=1, action="BUY", exchange=bc.exchange),
                         ComboLeg(conId=fc.conId, ratio=1, action="SELL", exchange=fc.exchange)]
        p["bag"] = bag
        p["tf"], p["tb"], p["tg"] = ib.reqMktData(fc), ib.reqMktData(bc), ib.reqMktData(bag)
    ib.sleep(a.wait)

    if a.snapshot:
        for regulatory in (False, True):
            todo = [(p, k) for p in plans for k in ("tf", "tb", "tg") if bad(p[k].bid) and bad(p[k].ask)]
            if not todo:
                break
            for p, k in todo:
                c = {"tf": p["fc"], "tb": p["bc"], "tg": p["bag"]}[k]
                ib.cancelMktData(c)
                p[k] = ib.reqMktData(c, "", True, regulatory)
            ib.sleep(max(a.wait, 6))

    # ---- price, check, stage
    to_place = []
    for p in plans:
        sym, fc, bc, qty = p["sym"], p["fc"], p["bc"], p["qty"]
        tf, tb, tg = p["tf"], p["tb"], p["tg"]
        action, n = ("BUY" if qty > 0 else "SELL"), abs(int(qty))
        mult = float(fc.multiplier or 1)
        tick = max(p["fcd"].minTick, p["bcd"].minTick)
        grid = float(p["pc"].get("spread_tick", SPREAD_GRID.get(sym, tick)))

        impl_bid = tb.bid - tf.ask if not (bad(tb.bid) or bad(tf.ask)) else float("nan")
        impl_ask = tb.ask - tf.bid if not (bad(tb.ask) or bad(tf.bid)) else float("nan")
        book_bid = tg.bid if not bad(tg.bid) and (tg.bidSize or 0) > 0 else float("nan")
        book_ask = tg.ask if not bad(tg.ask) and (tg.askSize or 0) > 0 else float("nan")

        if sym in manual:
            price, src = manual[sym], "manual --limit"
            if bad(book_bid) and bad(book_ask) and bad(impl_bid) and bad(impl_ask):
                src += "; NO QUOTES - cannot check whether it crosses, verify in TWS before walking"
        else:
            price, src = passive_price(action, book_bid, book_ask, impl_bid, impl_ask, grid,
                                       QUOTE_CLEAN.get(sym), a.ticks_away)

        rec = dict(symbol=sym, current=fc.localSymbol, next=bc.localSymbol, qty=qty, days=p["days"],
                   action=action, quantity=n, book_bid=book_bid, book_ask=book_ask, implied_bid=impl_bid,
                   implied_ask=impl_ask, limit=price, source=src, currency=fc.currency, staged=False)
        result["rolls"].append(rec)
        say("=" * 78)
        say(f"{sym}: hold {qty:+g} {fc.localSymbol} ({p['days']} days)  ->  roll to {bc.localSymbol}"
              + (f"   [{p['note']}]" if p["note"] else ""))
        say(f"  legs:   {'SELL' if qty > 0 else 'BUY'} {n} {fc.localSymbol}   +   "
              f"{'BUY' if qty > 0 else 'SELL'} {n} {bc.localSymbol}")
        back_now = sum(q for c, q in held.values() if c.conId == bc.conId)
        say(f"  after:  {fc.localSymbol} 0, {bc.localSymbol} {back_now + qty:+g}")
        say(f"  quotes: current {tf.bid}/{tf.ask}   next {tb.bid}/{tb.ask}")
        say(f"  spread (next - current): book {fmt(sym, book_bid)} / {fmt(sym, book_ask)} | "
              f"implied {fmt(sym, impl_bid)} / {fmt(sym, impl_ask)}")
        if price is None:
            say(f"  !! {src} - not staging (market shut? try --mdtype 2, --snapshot or --limit {sym}=...)")
            continue
        rec["crosses"] = crosses(action, price, book_bid, book_ask)
        if rec["crosses"]:
            say(f"  !! {fmt(sym, price)} would cross the spread book and fill on arrival - not staging. "
                f"This tool only places resting orders; walk the price in TWS instead.")
            continue
        cash = (-price if action == "BUY" else price) * mult * n
        rec["cash_at_limit"] = round(cash, 2)
        say(f"  order:  {action} {n} spread @ {fmt(sym, price)}  [{src}; grid {grid:g}, {a.tif}]")
        say(f"  cash at limit: {cash:+,.0f} {fc.currency} ({'credit' if cash > 0 else 'debit'})")

        ref_tag = f"ROLL {sym} {fc.localSymbol}>{bc.localSymbol}"
        if ref_tag in working_refs:
            say("  a working order with this tag already exists - skipping")
            continue
        o = LimitOrder(action, n, price, tif=a.tif, orderRef=ref_tag, transmit=True, account=account)
        try:
            st = ib.whatIfOrder(p["bag"], o)
            st = (st[0] if st else None) if isinstance(st, list) else st
            if st is None:
                say("  what-if: rejected or no answer from TWS (see error above) - not staging")
                continue
            rec.update(init_margin_change=float(st.initMarginChange),
                       maint_margin_change=float(st.maintMarginChange), commission=st.commission)
            say(f"  what-if margin change: init {float(st.initMarginChange):,.0f}  "
                  f"maint {float(st.maintMarginChange):,.0f}   commission ~{st.commission} {st.commissionCurrency}")
        except Exception as e:  # noqa: BLE001
            say(f"  what-if check failed ({e!r}) - not staging")
            continue
        rec["staged"] = True
        to_place.append((sym, p["bag"], o))

    if not to_place:
        say("\nnothing staged.")
        ib.disconnect()
        finish(1)
    if not a.live:
        say("\nDRY RUN - nothing sent. Re-run with --live to place.")
        ib.disconnect()
        finish(0)
    if not a.yes:
        if not sys.stdin.isatty():
            say("--live needs a person to type ROLL (or --yes after a human has reviewed the dry run)")
            ib.disconnect()
            finish(3)
        say(f"\nType ROLL to place {len(to_place)} order(s): ", end="", flush=True)
        if sys.stdin.readline().strip() != "ROLL":
            say("aborted")
            ib.disconnect()
            finish(3)
    for sym, bag, o in to_place:
        t = ib.placeOrder(bag, o)
        ib.sleep(2)
        msg = t.log[-1].message if t.log else ""
        result["placed"].append(dict(symbol=sym, ref=o.orderRef, status=t.orderStatus.status, message=msg))
        say(f"{sym}: {t.orderStatus.status}  {msg}")
    ib.disconnect()
    finish(0)

if __name__ == "__main__":
    main()
