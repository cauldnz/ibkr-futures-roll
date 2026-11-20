# ibkr-futures-roll

Roll your Interactive Brokers futures into the next contract month with one command. Each roll is placed as a single calendar-spread order that **waits at the bid or ask** rather than filling straight away. You then nudge the price yourself in TWS or the IBKR app until it fills.

Nothing is sent unless you ask for it: the default is a dry run that shows you what it would do.

> **No warranty.** Tested on one IBKR account in November 2026. Always read the dry run and check the orders in TWS. You are responsible for your own orders.

*Using an AI agent such as Claude Code? Point it at [AGENTS.md](AGENTS.md).*

## Set up (once)

1. **Install Python 3.11 or newer**, then either:
   - install [uv](https://docs.astral.sh/uv/) (it handles the rest), or
   - run `pip install ib_async`.
2. **Turn on the API in TWS:** File → Global Configuration → API → Settings.
   - Tick **Enable ActiveX and Socket Clients**.
   - Untick **Read-Only API**.
   - Set **Socket port** to 7496 (live) or 7497 (paper).
   - Leave **Allow connections from localhost only** ticked.
3. **Optional:** copy `roll.example.toml` to `roll.toml` and change it to suit you (see below).

## Use it

Run these in a terminal, in the folder with the script. If you didn't install uv, use `python` instead of `uv run`.

| You want to… | Run |
|---|---|
| See what's due to roll | `uv run roll_futures.py --list` |
| See the prices and orders, without sending anything | `uv run roll_futures.py` |
| Do just some contracts | `uv run roll_futures.py --only ZN ZT` |
| Send the orders | add `--live`, then type `ROLL` when asked |
| Check whether they've filled | `uv run roll_futures.py --status` |

Once the orders are in, find them in TWS or the IBKR app under Orders. They're labelled **ROLL …** and IBKR shows them as "Buy/Sell … Reverse Calendar". Move the price a step at a time until they fill.

## Reading the dry run

For each contract you'll see:
- **legs**: the actual trades, for example `SELL 10 ZNZ6 + BUY 10 ZNH7`.
- **after**: your position once the roll is done.
- **order**: the price it will wait at, and where that price came from.
- **cash at limit**: what you receive (credit) or pay (debit) if it fills at that price.
- **what-if**: TWS's estimate of the margin change and commission.

If any line doesn't look right, don't send it.

## Your settings file (`roll.toml`)

Everything in it is optional. Use it to:
- **roll two contracts together** (`pairs`), for example the two legs of a curve trade.
- **pick the month to roll into** (`back_month`). The default is the next listed month, which isn't always the busy one.
- **roll earlier** (`within_days`). Physically delivered futures, such as Treasuries and gold, must be rolled before *first notice day*, which can be weeks before the contract stops trading.
- **keep a ladder of months** (`ladder_months`). For example, `3` keeps the position spread equally over the next three months.
  - When the front month comes due, it rolls into the month after the last one.
  - If the months would end up uneven, the roll also evens them out.
- **leave a contract alone** (`skip = true`).

`roll.example.toml` shows each one.

## If something goes wrong

| You see | It means |
|---|---|
| `could not connect` | TWS isn't running, or the API isn't turned on (see set-up step 2). |
| `no usable quote` | That market is shut, or you don't have live data for it. Try again when it's open. You can also try `--snapshot`, or set the price yourself with `--limit SYMBOL=PRICE`. |
| `Error 110 … minimum price variation` | The spread can't be priced in that step. Nothing was sent. Please open an issue saying which contract. |
| `You must specify an account` | Add `--account U1234567`. |
| `would cross the spread book … not staging` | Your `--limit` price would fill straight away. This tool only places orders that wait. Use a price on your side of the market, or trade it by hand in TWS. |

## Safety built in

- Nothing is sent without `--live`, and then only after you type `ROLL`.
- It never places an order that would fill on arrival.
- An order is only sent after TWS's own margin check has accepted it.
- Running it again won't duplicate an order that is already working.
- It only connects to TWS on your own machine, unless you give it `--host`.
- It never asks for or stores passwords, and doesn't send anything over the internet itself.

## Licence

MIT. See [LICENSE](LICENSE). Tests: `python tests/test_roll.py`. They run against a simulated TWS, with no account needed.
