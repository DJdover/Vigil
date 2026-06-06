# Vigil

A tiny language for **honestly** scoring betting and prediction-market strategies.

Vigil has one job: make the *dishonest* version of a result impossible to express. Most ways a backtest lies to you aren't bugs — they're choices the analyst makes without noticing. Vigil turns each of those choices into a rule the language enforces, so the lie becomes something you literally cannot write.

```
strategy demo {
    registered 2026-01-08    # the pre-registration date
    forward_only             # scoring earlier data is an error
    unit event               # correlated bets on one event count once
    pnl canonical            # exactly one P&L definition
    min_units 30             # below this, no verdict — too thin to conclude
    coverage min 0.60        # must cover >=60% of the slate to be representative
    significance 0.95        # per-unit EV must clear a bootstrap lower bound
}

bets from "demo.csv"         # date,event,side,price,won[,prob]

report demo
```

Run it:

```
python3 vigil.py lock examples/demo_thin.vig    # pre-register (write the .lock)
python3 vigil.py examples/demo_thin.vig         # score it
python3 tests.py                                # run the test suite
```

## What it is (and isn't)

Vigil evaluates whether an edge is **real**. It is deliberately *not* a tipster — it generates no picks and no models; you bring your own probabilities. And it is *not* a bankroll manager — it does no bet-sizing. It tells you whether to believe a result, not what to bet. That boundary is the point: it's an honesty tool, not a gambling aid.

## The failures it prevents

- **In-sample scoring.** `forward_only` drops anything before `registered` and *hard errors* if you try to score pre-registration data. You can't grade your own training set.
- **Correlated bets.** Several bets on one event are one outcome. `unit event` clusters them, so a single lucky event can't pose as a winning streak.
- **P&L ambiguity.** `pnl canonical` is the only definition there is; it can't disagree with itself across the file.
- **Too small to conclude.** `min_units N` withholds the verdict below the floor — not positive, not negative, just *not enough data to say.*
- **Non-representative coverage.** Declare the `slate` (the universe you could have acted on); below `coverage min`, the verdict is withheld. A strategy that only ever saw a third of the slate isn't a measurement.
- **The win-rate mirage.** `report` never prints a win rate without EV beside it, and always shows average win vs average loss — so "won 60%!" can't hide "but lost more per loss than it made per win."
- **A point estimate posing as an edge.** `significance C` runs a by-unit bootstrap; if the lower bound on per-unit EV is `<= 0`, the result isn't distinguishable from zero and Vigil refuses to call it positive even when the average is up.
- **An overconfident model.** Add a `prob` column (your model's probability per bet) and Vigil reports the edge you *claimed* and checks it against what actually happened — "claimed win-rate 70% vs actual 52% — overconfident by 18 pts."

## Verdicts

- `NO VERDICT` — underpowered or coverage too thin. Honest abstention.
- `NO EDGE` — per-unit EV `<= 0`, or positive but not significant.
- `POSITIVE` — forward, per-unit EV `> 0`, and clears the significance bound.

## The integrity model

The thresholds only mean something if the strategy block is committed **before** you look at the data. `vigil lock <file.vig>` writes a hash of the strategy block to `<file.vig>.lock`; commit it alongside the `.vig` and timestamp it (e.g. via OpenTimestamps). On every run Vigil recomputes the hash and **refuses to score if the block changed** — because lowering `min_units` until a `NO VERDICT` becomes a `POSITIVE` is goalpost-moving. Vigil enforces the rules; the lock is what stops you from quietly rewriting them.

## Roadmap (genuine future work, deliberately not in v1)

- Multiple strategies in one file with a Benjamini–Hochberg FDR correction (the "tried fifty, one looked good" trap).
- A block bootstrap for serially-correlated units.
- Typed `price` vs `prob` as a full expression layer for bet *selection*.

Not on the roadmap, on purpose: pick generation and bet sizing. See "What it is (and isn't)."

## License

MIT. See `LICENSE`.
