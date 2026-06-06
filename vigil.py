#!/usr/bin/env python3
"""
Vigil — a tiny language for HONESTLY scoring betting / prediction-market strategies.

Vigil's one job: make the dishonest version of a result impossible to express.
Each rule below is a real way people fool themselves, turned into something the
language won't let you say.

SCOPE (on purpose): Vigil evaluates whether an edge is *real*. It is not a tipster
(it generates no picks or models — you bring your own probabilities) and not a
bankroll manager (it does no bet-sizing). It tells you whether to believe a result,
not what to bet.

Rules:
  forward_only        scoring data that predates registration is an error
  unit <kind>         results cluster by unit, so correlated bets count once
  pnl canonical       exactly ONE P&L definition, applied everywhere
  min_units N         below N units the verdict is withheld — too thin to conclude
  coverage min X      if you covered < X of the declared slate, sample isn't
                      representative and the verdict is withheld
  significance C      a by-unit bootstrap; if the C lower bound on per-unit EV is
                      <= 0, the result isn't distinguishable from zero -> no edge
  (optional) prob     per-bet model probability -> Vigil reports your claimed edge
                      and checks whether your model was calibrated or overconfident
  report              win-rate is never shown without EV; avg-win vs avg-loss is
                      always shown; in-sample / insufficient / insignificant results
                      can never be labeled "positive"

Integrity: the thresholds only mean something if the strategy block is committed
BEFORE you look at data. `vigil lock` writes a hash of the strategy block; on every
run Vigil checks it and refuses to score if the block changed (editing thresholds
after seeing results to flip a verdict is goalpost-moving).

Usage:
  python3 vigil.py <file.vig>          run
  python3 vigil.py run <file.vig>      run
  python3 vigil.py lock <file.vig>     write <file.vig>.lock (the pre-registration)
"""
import sys, csv, os, random, hashlib
from collections import defaultdict, Counter


class VigilError(Exception):
    pass


def _bool(s):
    return str(s).strip().lower() in ('true', 't', '1', 'yes', 'won')


def _unit_interval(x, what):
    v = float(x)
    if not (0.0 <= v <= 1.0):
        raise VigilError(f"{what} must be in [0,1], got {v}")
    return v


def load_csv_bets(path):
    out = []
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            b = {'date': row['date'].strip(), 'event': row['event'].strip(),
                 'side': row['side'].strip().upper(),
                 'price': _unit_interval(row['price'], 'price'),
                 'won': _bool(row['won'])}
            if row.get('prob') not in (None, ''):
                b['prob'] = _unit_interval(row['prob'], 'prob')
            out.append(b)
    return out


def parse(text, base_dir):
    strat, bets, slate = {}, [], {}
    report_target, bets_from = None, None
    lines = [ln for ln in (raw.split('#', 1)[0].strip()
             for raw in text.splitlines()) if ln]

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('strategy'):
            head = line.split()
            if len(head) < 2 or not line.endswith('{'):
                raise VigilError(f"malformed strategy header: {line}")
            strat['name'] = head[1]
            i += 1
            while i < len(lines) and lines[i] != '}':
                parts = lines[i].split()
                key = parts[0]
                if key == 'registered':
                    strat['registered'] = parts[1].strip('"')
                elif key == 'forward_only':
                    strat['forward_only'] = True
                elif key == 'unit':
                    strat['unit'] = parts[1]
                elif key == 'pnl':
                    strat['pnl'] = parts[1]
                elif key == 'min_units':
                    strat['min_units'] = int(parts[1])
                elif key == 'coverage':
                    if len(parts) >= 3 and parts[1] == 'min':
                        strat['coverage_min'] = float(parts[2])
                    else:
                        raise VigilError(f"bad coverage spec: {lines[i]}")
                elif key == 'significance':
                    strat['significance'] = float(parts[1])
                else:
                    raise VigilError(f"unknown strategy field: {key}")
                i += 1
        elif line.startswith('slate'):
            i += 1
            while i < len(lines) and lines[i] != '}':
                row = [c.strip() for c in lines[i].split('|')]
                if len(row) != 2:
                    raise VigilError(f"bad slate row (need date | expected): {lines[i]}")
                slate[row[0]] = int(row[1])
                i += 1
        elif line.startswith('bets'):
            if 'from' in line:
                bets_from = line.split('from', 1)[1].strip().strip('"').strip("'")
            else:
                i += 1
                while i < len(lines) and lines[i] != '}':
                    row = [c.strip() for c in lines[i].split('|')]
                    if row and row[0].lower() != 'date' and len(row) >= 5:
                        b = {'date': row[0], 'event': row[1], 'side': row[2].upper(),
                             'price': _unit_interval(row[3], 'price'), 'won': _bool(row[4])}
                        if len(row) >= 6 and row[5] != '':
                            b['prob'] = _unit_interval(row[5], 'prob')
                        bets.append(b)
                    i += 1
        elif line.startswith('report'):
            report_target = line.split()[1]
        i += 1

    if bets_from:
        bets = load_csv_bets(os.path.join(base_dir, bets_from))
    return strat, bets, slate, report_target


def strategy_block_hash(text):
    """Canonical hash of the strategy block (field lines only, order-independent)."""
    lines = [ln for ln in (raw.split('#', 1)[0].strip()
             for raw in text.splitlines()) if ln]
    block, inside = [], False
    for ln in lines:
        if ln.startswith('strategy'):
            inside = True
            block.append('strategy{')
            continue
        if inside and ln == '}':
            break
        if inside:
            block.append(ln)
    return hashlib.sha256('\n'.join(sorted(block)).encode()).hexdigest()


def pnl(bet):
    # ONE definition, in one place. Per-contract: win -> 1-price, loss -> -price.
    return (1.0 - bet['price']) if bet['won'] else -bet['price']


def bootstrap_lower_bound(unit_pnls, conf, B=4000, seed=0):
    rnd = random.Random(seed)
    n = len(unit_pnls)
    means = [sum(unit_pnls[rnd.randrange(n)] for _ in range(n)) / n for _ in range(B)]
    means.sort()
    return means[int((1.0 - conf) / 2.0 * B)]


def run(text, base_dir, lock_path=None):
    strat, bets, slate, target = parse(text, base_dir)

    if 'name' not in strat:
        raise VigilError("no strategy block found")
    if strat.get('pnl') != 'canonical':
        raise VigilError("strategy must declare `pnl canonical` — Vigil allows exactly one P&L definition")
    if target is None:
        raise VigilError("no `report <strategy>` statement")
    if target != strat['name']:
        raise VigilError(f"report target '{target}' does not match strategy '{strat['name']}'")

    lock_note = None
    if lock_path and os.path.exists(lock_path):
        want = open(lock_path).read().strip()
        if strategy_block_hash(text) != want:
            raise VigilError("strategy block changed since it was locked — thresholds edited after "
                             "pre-registration? re-locking after seeing results is goalpost-moving. "
                             "(delete the .lock only if you mean to start a new pre-registration.)")
        lock_note = "locked & unchanged"
    elif lock_path:
        lock_note = "UNLOCKED (not pre-registered — run `vigil lock` before you look at data)"

    reg = strat.get('registered')
    excluded = 0
    if strat.get('forward_only'):
        if not reg:
            raise VigilError("forward_only requires a `registered` date")
        used = [b for b in bets if b['date'] >= reg]
        excluded = len(bets) - len(used)
    else:
        if reg and any(b['date'] < reg for b in bets):
            raise VigilError(f"cannot report '{strat['name']}': data includes bets before "
                             f"registration ({reg}) — that is in-sample. declare `forward_only`.")
        used = bets
    if not used:
        raise VigilError("no forward bets to score yet")

    unit_kind = strat.get('unit', 'bet')
    if unit_kind == 'bet':
        groups = {i: [b] for i, b in enumerate(used)}
    else:
        groups = defaultdict(list)
        for b in used:
            groups[(b['date'], b['event'])].append(b)

    unit_pnls = [sum(pnl(b) for b in g) for g in groups.values()]
    n = len(unit_pnls)
    total = sum(unit_pnls)
    per_unit = total / n
    wins = [p for p in unit_pnls if p > 0]
    losses = [p for p in unit_pnls if p <= 0]
    win_rate = len(wins) / n
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    cov_ok, cov_overall, cov_lo, cov_hi = True, None, None, None
    if slate:
        obs = Counter(b['date'] for b in used) if unit_kind == 'bet' \
            else Counter(d for (d, e) in groups.keys())
        tot_obs = tot_exp = 0
        pcts = []
        for d in sorted(slate):
            exp = slate[d]
            o = obs.get(d, 0)
            tot_obs += o
            tot_exp += exp
            pcts.append(o / exp if exp else 0.0)
        cov_overall = tot_obs / tot_exp if tot_exp else 0.0
        cov_lo, cov_hi = min(pcts), max(pcts)
        cmin = strat.get('coverage_min')
        if cmin is not None and cov_overall < cmin:
            cov_ok = False

    conf = strat.get('significance')
    lb = bootstrap_lower_bound(unit_pnls, conf) if conf else None

    probed = [b for b in used if 'prob' in b]
    edge_line = None
    if probed:
        claimed_edge = sum(b['prob'] - b['price'] for b in probed) / len(probed)
        claimed_wr = sum(b['prob'] for b in probed) / len(probed)
        actual_wr = sum(1 for b in probed if b['won']) / len(probed)
        edge_line = (len(probed), claimed_edge, claimed_wr, actual_wr)

    print(f"=== vigil report: {strat['name']} ===")
    if lock_note:
        print(f"pre-registration: {lock_note}")
    print(f"registered {reg} | forward_only={bool(strat.get('forward_only'))} "
          f"| unit={unit_kind} | pnl=canonical")
    if excluded:
        print(f"dropped {excluded} in-sample bet(s) before {reg}")
    print(f"bets {len(used)} -> units {n}")
    print(f"P&L {total:+.2f} | per-unit EV {per_unit:+.4f} | unit win-rate {win_rate:.0%}")
    print(f"avg win {avg_win:+.3f} | avg loss {avg_loss:+.3f}  (win-small / lose-big check)")
    if edge_line:
        m, ce, cw, aw = edge_line
        tail = f" — model overconfident by {(cw - aw) * 100:.0f} pts" if (cw - aw) * 100 > 1 else ""
        print(f"model edge: claimed {ce:+.3f}/bet on {m} bets | "
              f"claimed win-rate {cw:.0%} vs actual {aw:.0%}{tail}")
    if cov_overall is not None:
        tail = f" — below required {strat['coverage_min']:.0%}" if not cov_ok else ""
        print(f"coverage {cov_overall:.0%} of slate (per-day {cov_lo:.0%}-{cov_hi:.0%}){tail}")
    if lb is not None:
        print(f"per-unit EV {int(conf * 100)}% bootstrap lower bound {lb:+.4f}")

    reasons = []
    mu = strat.get('min_units')
    if mu is not None and n < mu:
        reasons.append(f"underpowered ({n} units < min_units {mu})")
    if not cov_ok:
        reasons.append(f"coverage {cov_overall:.0%} < required {strat['coverage_min']:.0%} (sample not representative)")

    if reasons:
        print("verdict: NO VERDICT — " + "; ".join(reasons)
              + ". Not positive, not negative — not enough honest data to say.")
    elif per_unit <= 0:
        print("verdict: NO EDGE — per-unit EV <= 0. Vigil will not call this positive.")
    elif lb is not None and lb <= 0:
        print(f"verdict: NO EDGE — per-unit EV is +{per_unit:.4f} but its {int(conf * 100)}% lower "
              f"bound is {lb:+.4f} <= 0, not distinguishable from zero. Vigil will not call this positive.")
    else:
        print("verdict: POSITIVE (forward, per-unit EV > 0 and significant)")


def lock(path):
    h = strategy_block_hash(open(path).read())
    open(path + '.lock', 'w').write(h + '\n')
    print(f"locked {path} -> {path}.lock ({h[:16]}…)")
    print("commit the .lock alongside the .vig and timestamp it. editing the strategy block now is caught.")


def main(argv):
    if len(argv) == 3 and argv[1] == 'lock':
        lock(argv[2]); return
    if len(argv) == 3 and argv[1] == 'run':
        path = argv[2]
    elif len(argv) == 2:
        path = argv[1]
    else:
        print("usage: python3 vigil.py <file.vig> | run <file.vig> | lock <file.vig>")
        sys.exit(2)
    try:
        run(open(path).read(), os.path.dirname(os.path.abspath(path)), lock_path=path + '.lock')
    except VigilError as e:
        print(f"vigil: error — {e}")
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv)
