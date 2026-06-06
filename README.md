# Vigil

A tiny language for **honestly** scoring betting and prediction-market strategies.

Vigil has one job: make the *dishonest* version of a result impossible to express. Most ways a backtest lies to you aren't bugs — they're choices the analyst makes without noticing. Vigil turns each of those choices into a rule the language enforces, so the lie becomes something you literally cannot write.

```
fdr 0.10                     # control false-discovery rate across ALL strategies below

strategy demo {
    registered 2026-01-08    # the pre-registration date
    forward_only             # scoring earlier data is an error
    unit event               # correlated bets on one event count once
    pnl canonical            # exactly one P&L definition
    min_units 30             # below this, no verdict — too thin to conclude
    coverage min 0.60        # must cover >=60% of the slate to be representative
    significance 0.95        # per-unit EV must clear a bootstrap lower bound
    block 1                  # moving-block bootstrap size (raise for serial correlation)
}

slate {                      # the universe you could have acted on, per day
    2026-01-08 | 20
}

bets from "demo.csv"         # date,event,side,price,won[,prob]

report demo
```

Run it:

```
python3 vigil.py lock examples/demo_batch.vig    # pre-register (writes the .lock)
python3 vigil.py lock examples/demo_batch.vig --ots   # ...and OpenTimestamp it (needs `ots`)
python3 vigil.py examples/demo_batch.vig         # score it
python3 vigil.py verify examples/demo_batch.vig  # re-check the lock (+ .ots if present)
python3 tests.py                                 # run the test suite
```

## What it is (and isn't)

Vigil evaluates whether an edge is **real**. It is deliberately *not* a tipster — it generates no picks and no models; you bring your own probabilities. And it is *not* a bankroll manager — it does no bet-sizing. It tells you whether to believe a result, not what to bet. That boundary is the point: it's an honesty tool, not a gambling aid.

## The failures it prevents

- **In-sample scoring.** `forward_only` drops anything before `registered` and *hard errors* if you try to score pre-registration data. You can't grade your own training set.
- **Correlated bets.** Several bets on one event are one outcome. `unit event` clusters them, so a single lucky event can't pose as a winning streak.
- **P&L ambiguity.** `pnl canonical` is the only definition there is; it can't disagree with itself across the file.
- **Too small to conclude.** `min_units N` withholds the verdict below the floor — not positive, not negative, just *not enough data to say.*
- **Non-representative coverage.** Declare the `slate`; below `coverage min`, the verdict is withheld. A strategy that only ever saw a third of the slate isn't a measurement.
- **The win-rate mirage.** `report` never prints a win rate without EV beside it, and always shows average win vs average loss — so "won 60%!" can't hide "but lost more per loss than it made per win."
- **A point estimate posing as an edge.** `significance C` runs a by-unit bootstrap; if the lower bound on per-unit EV is `<= 0`, the result isn't distinguishable from zero and Vigil refuses to call it positive even when the average is up.
- **Serial correlation.** `block K` switches the bootstrap to a moving-block resample of `K` consecutive units, so runs of correlated days don't masquerade as independent evidence.
- **The multiplicity trap — "we tried fifty, one looked good."** Declare `fdr Q` and Vigil applies a Benjamini-Hochberg correction across *every* strategy in the file. A strategy that looks positive on its own is refused if it doesn't survive the correction for how many were tried. (See `examples/demo_batch.vig`: a `marginal` strategy is `+0.10/unit` and looks positive alone, but **fails FDR** once you count that four were run.)
- **An overconfident model.** Add a `prob` column and Vigil reports the edge you *claimed*, a calibration **reliability curve + ECE**, and an overconfidence flag — "claimed win-rate 70% vs actual 56% — overconfident by 14 pts."

## Verdicts

- `NO VERDICT` — underpowered or coverage too thin. Honest abstention.
- `NO EDGE` — per-unit EV `<= 0`, or positive but not significant, or fails FDR across the batch.
- `POSITIVE` — forward, per-unit EV `> 0`, clears significance, and survives FDR.

## The integrity model — and its honest limit

The thresholds only mean something if the **whole protocol** — every strategy block, every slate, and the `fdr` setting — is committed *before* you look at the data. `vigil lock <file>` writes a hash of that protocol to `<file>.lock` and **refuses to score if it changes** — editing a threshold, shrinking a slate, or adding/removing strategies after seeing results is goalpost-moving. `vigil lock <file> --ots` (or `ots stamp <file>.lock`) anchors that hash in time via OpenTimestamps; `vigil verify <file>` re-checks the hash and the timestamp.

Locking the *whole file* is what makes FDR honest: Benjamini-Hochberg only controls the strategies you declared, so the timestamp over all of them is the proof you didn't hide the losers.

**What Vigil cannot do — said plainly.** It verifies a result is *internally* honest. It cannot verify your *inputs*: the registration date, the slate, and the data are things you declare, and Vigil takes them on faith. A determined cheater can still feed it fiction. The lock + an OpenTimestamps proof over the full pre-registered file close that gap as far as any tool can — they make "I committed these exact rules, this slate, and this whole batch, before this date" checkable by anyone — but they cannot make your data true. Vigil is an honesty tool for someone who wants to be honest, not a fraud-proof oracle.

## Pre-registration in practice

```
$ vigil lock demo_batch.vig
locked demo_batch.vig -> demo_batch.vig.lock (45b14a32…)
this hash covers the whole pre-registered protocol: every strategy block, slate, and the
fdr setting — but NOT the bet data. commit the .lock alongside the .vig.
to anchor this hash in time: `vigil lock demo_batch.vig --ots`  (or: ots stamp demo_batch.vig.lock)

$ vigil verify demo_batch.vig
OK — protocol matches the lock (45b14a32…)

# ...later, someone quietly loosens a threshold after seeing the data:
$ vigil verify demo_batch.vig          # exits non-zero
MISMATCH — protocol hash 617be964… != locked 45b14a32…
the pre-registered protocol changed since locking. this is the goalpost-moving guard.
```

The `.lock` is yours to commit and timestamp — it is intentionally **not** shipped in this repo, so the examples stay runnable from a clean state and so the lock means *your* pre-registration, not ours.

## Roadmap (genuine future work, deliberately not in v1)

- A typed `price` vs `prob` expression layer for bet *selection* (so selection logic is part of the locked protocol too).
- Per-strategy registered dates with a staggered-holdout report.
- Richer calibration output (per-bin counts already shown; significance bands on the reliability curve next).

Not on the roadmap, on purpose: pick generation and bet sizing. See "What it is (and isn't)."

## License

MIT. See `LICENSE`.

## The Oddvane research program

Vigil is the discipline behind a set of pre-registered, falsification-first studies of how prediction markets move and how well they're calibrated — published whether or not the result is exciting (so far, mostly not — which is the point).

- [Oddvane-study-A](https://github.com/DJdover/Oddvane-study-A) — cross-venue lead-lag on championship futures. **No robust lead.**
- [Oddvane-study-A2](https://github.com/DJdover/Oddvane-study-A2) — cross-venue lead-lag on 871 live in-game markets. **No robust lead.**
- [Oddvane-study-D](https://github.com/DJdover/Oddvane-study-D) — cross-venue "who's right" calibration edge. **No edge** — the venues are equally calibrated.
- **Vigil** (this repo) — the pre-registration + falsification discipline those studies run on, as a reusable tool.
