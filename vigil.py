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

What Vigil CAN and CANNOT do: it verifies the result is *internally* honest — no
in-sample scoring, no correlated double-counting, no win-rate-without-EV, no
under-powered or thinly-covered claim, no point-estimate masquerading as an edge,
and (across a batch) no cherry-picked winner. It CANNOT verify your inputs: the
registration date, the slate, and the data are things you declare. The lock + an
OpenTimestamps proof over the whole pre-registered file is what closes that gap as
far as any tool can — see `lock` / `verify`.

Strategy rules:
  forward_only        scoring data that predates registration is an error
  unit <kind>         results cluster by unit, so correlated bets count once
  pnl canonical       exactly ONE P&L definition, applied everywhere
  min_units N         below N units the verdict is withheld — too thin to conclude
  coverage min X      if you covered < X of the declared slate, sample isn't
                      representative and the verdict is withheld
  significance C      a by-unit bootstrap; if the C lower bound on per-unit EV is
                      <= 0, the result isn't distinguishable from zero -> no edge
  block K             (optional) moving-block bootstrap of K consecutive units, for
                      serially-correlated units; default 1 = i.i.d.
  (optional) prob     per-bet model probability -> Vigil reports your claimed edge,
                      checks calibration (reliability curve + ECE), and flags
                      overconfidence
File-level:
  fdr Q               control the false-discovery rate across ALL strategies in the
                      file (Benjamini-Hochberg). Defeats "run fifty, publish the one
                      that passed" — but ONLY for the strategies actually in the
                      file, which is why the whole file must be locked + timestamped.

Integrity: the thresholds only mean something if the pre-registered protocol — every
strategy block, the slate(s), and the fdr setting — is committed BEFORE you look at
data. `vigil lock` writes a hash of that protocol; on every run Vigil checks it and
refuses to score if it changed (editing thresholds, slates, or adding/removing
strategies after seeing results is goalpost-moving). `vigil lock --ots` (or stamping
the .lock yourself) anchors that hash in time; `vigil verify` re-checks both.

Usage:
  python3 vigil.py <file.vig>          run
  python3 vigil.py run <file.vig>      run
  python3 vigil.py lock <file.vig>     write <file.vig>.lock (the pre-registration)
  python3 vigil.py lock <file.vig> --ots   also OpenTimestamp the .lock (needs `ots`)
  python3 vigil.py verify <file.vig>   check the .lock (and .lock.ots if present)
"""
import sys, csv, os, random, hashlib, shutil, subprocess, datetime
from collections import defaultdict, Counter


class VigilError(Exception):
    pass


# ---------------------------------------------------------------- helpers ----
def _bool(s):
    return str(s).strip().lower() in ('true', 't', '1', 'yes', 'won')


def _unit_interval(x, what):
    v = float(x)
    if not (0.0 <= v <= 1.0):
        raise VigilError(f"{what} must be in [0,1], got {v}")
    return v


def _date(s, what):
    """ISO YYYY-MM-DD only — parsed as a real date, never compared lexically."""
    try:
        return datetime.date.fromisoformat(str(s).strip())
    except ValueError:
        raise VigilError(f"{what} must be an ISO date (YYYY-MM-DD), got {s!r}")


def load_csv_bets(path):
    out = []
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            b = {'date': _date(row['date'], 'bet date'), 'event': row['event'].strip(),
                 'side': row['side'].strip().upper(),
                 'price': _unit_interval(row['price'], 'price'),
                 'won': _bool(row['won'])}
            if row.get('prob') not in (None, ''):
                b['prob'] = _unit_interval(row['prob'], 'prob')
            out.append(b)
    return out


# ---------------------------------------------------------------- parsing ----
def _strip_lines(text):
    return [ln for ln in (raw.split('#', 1)[0].strip() for raw in text.splitlines()) if ln]


def parse(text, base_dir):
    """Parse a file into (fdr, [strategy,...]). Each strategy carries its own
    fields, optional slate, bets source, and whether it was `report`ed. slate /
    bets / report attach to the most recent strategy."""
    lines = _strip_lines(text)
    fdr = None
    strategies = []
    cur = None
    i = 0
    while i < len(lines):
        line = lines[i]
        head = line.split()
        kw = head[0]
        if kw == 'fdr':
            fdr = float(head[1])
            i += 1
        elif kw == 'strategy':
            if len(head) < 2 or not line.endswith('{'):
                raise VigilError(f"malformed strategy header: {line}")
            cur = {'name': head[1], 'slate': {}, 'bets': [], 'bets_from': None, 'reported': False}
            strategies.append(cur)
            i += 1
            while i < len(lines) and lines[i] != '}':
                p = lines[i].split()
                k = p[0]
                if k == 'registered':
                    cur['registered'] = _date(p[1].strip('"'), 'registered')
                elif k == 'forward_only':
                    cur['forward_only'] = True
                elif k == 'unit':
                    cur['unit'] = p[1]
                elif k == 'pnl':
                    cur['pnl'] = p[1]
                elif k == 'min_units':
                    cur['min_units'] = int(p[1])
                elif k == 'coverage':
                    if len(p) >= 3 and p[1] == 'min':
                        cur['coverage_min'] = float(p[2])
                    else:
                        raise VigilError(f"bad coverage spec: {lines[i]}")
                elif k == 'significance':
                    cur['significance'] = float(p[1])
                elif k == 'block':
                    cur['block'] = int(p[1])
                else:
                    raise VigilError(f"unknown strategy field: {k}")
                i += 1
            i += 1  # skip '}'
        elif kw == 'slate':
            if cur is None:
                raise VigilError("`slate` before any `strategy`")
            i += 1
            while i < len(lines) and lines[i] != '}':
                row = [c.strip() for c in lines[i].split('|')]
                if len(row) != 2:
                    raise VigilError(f"bad slate row (need date | expected): {lines[i]}")
                cur['slate'][_date(row[0], 'slate date')] = int(row[1])
                i += 1
            i += 1
        elif kw == 'bets':
            if cur is None:
                raise VigilError("`bets` before any `strategy`")
            if 'from' in line:
                cur['bets_from'] = line.split('from', 1)[1].strip().strip('"').strip("'")
                i += 1
            else:
                i += 1
                while i < len(lines) and lines[i] != '}':
                    row = [c.strip() for c in lines[i].split('|')]
                    if row and row[0].lower() != 'date' and len(row) >= 5:
                        b = {'date': _date(row[0], 'bet date'), 'event': row[1],
                             'side': row[2].upper(), 'price': _unit_interval(row[3], 'price'),
                             'won': _bool(row[4])}
                        if len(row) >= 6 and row[5] != '':
                            b['prob'] = _unit_interval(row[5], 'prob')
                        cur['bets'].append(b)
                    i += 1
                i += 1
        elif kw == 'report':
            name = head[1]
            match = next((s for s in strategies if s['name'] == name), None)
            if match is None:
                raise VigilError(f"report target '{name}' does not match any strategy")
            match['reported'] = True
            i += 1
        else:
            raise VigilError(f"unexpected statement: {line}")

    for s in strategies:
        if s['bets_from']:
            s['bets'] = load_csv_bets(os.path.join(base_dir, s['bets_from']))
    return fdr, strategies


def registration_hash(text):
    """Canonical hash of the PRE-REGISTERED PROTOCOL: every strategy block, every
    slate, the fdr line, the bets-source and report statements — everything EXCEPT
    inline bet data rows (the data comes after registration) and comments. Order
    matters (multiple strategies), so lines are kept in order, only whitespace- and
    comment-normalized. This is what `lock` commits and what an OTS proof anchors."""
    lines = _strip_lines(text)
    out, in_bets_inline = [], False
    for ln in lines:
        if ln.startswith('bets') and 'from' not in ln and ln.endswith('{'):
            in_bets_inline = True
            out.append('bets{')
            continue
        if in_bets_inline:
            if ln == '}':
                in_bets_inline = False
            continue  # drop inline bet DATA rows from the protocol hash
        out.append(' '.join(ln.split()))
    return hashlib.sha256('\n'.join(out).encode()).hexdigest()


# ------------------------------------------------------------- statistics ----
def pnl(bet):
    # ONE definition, in one place. Per-contract: win -> 1-price, loss -> -price.
    return (1.0 - bet['price']) if bet['won'] else -bet['price']


def _resample(units, k, rnd):
    """One bootstrap resample. k>1 -> moving-block bootstrap of K consecutive units
    (preserves serial correlation); k<=1 -> i.i.d."""
    n = len(units)
    if k <= 1 or n < k:
        return [units[rnd.randrange(n)] for _ in range(n)]
    blocks = [units[i:i + k] for i in range(0, n - k + 1)]
    out = []
    while len(out) < n:
        out.extend(blocks[rnd.randrange(len(blocks))])
    return out[:n]


def bootstrap(unit_pnls, conf, block=1, B=4000, seed=0):
    """Returns (lower_bound, p_value) for H0: per-unit EV <= 0, one-sided."""
    rnd = random.Random(seed)
    n = len(unit_pnls)
    means = sorted(sum(r) / n for r in (_resample(unit_pnls, block, rnd) for _ in range(B)))
    lb = means[int((1.0 - conf) / 2.0 * B)] if conf else None
    p = (1 + sum(1 for m in means if m <= 0)) / (B + 1)
    return lb, p


def reliability(probed, n_bins=10):
    """Reliability curve + ECE over (prob, won). Textbook; standalone (no imports)."""
    bins = defaultdict(list)
    for b in probed:
        bins[min(n_bins - 1, int(b['prob'] * n_bins))].append(b)
    curve, ece, N = [], 0.0, len(probed)
    for idx in sorted(bins):
        g = bins[idx]
        mp = sum(b['prob'] for b in g) / len(g)
        wr = sum(1 for b in g if b['won']) / len(g)
        curve.append((mp, wr, len(g)))
        ece += len(g) / N * abs(mp - wr)
    return curve, ece


# --------------------------------------------------------------- evaluate ----
def evaluate(strat):
    """Compute everything for one strategy EXCEPT the FDR-dependent final call.
    Returns a result dict with status in {'no_verdict','eligible'} and metrics."""
    if strat.get('pnl') != 'canonical':
        raise VigilError(f"strategy '{strat['name']}' must declare `pnl canonical`")
    bets = strat['bets']
    reg = strat.get('registered')
    excluded = 0
    if strat.get('forward_only'):
        if not reg:
            raise VigilError(f"'{strat['name']}': forward_only requires a `registered` date")
        used = [b for b in bets if b['date'] >= reg]
        excluded = len(bets) - len(used)
    else:
        if reg and any(b['date'] < reg for b in bets):
            raise VigilError(f"cannot report '{strat['name']}': data includes bets before "
                             f"registration ({reg}) — that is in-sample. declare `forward_only`.")
        used = bets
    if not used:
        raise VigilError(f"'{strat['name']}': no forward bets to score yet")

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
    slate = strat['slate']
    if slate:
        obs = Counter(b['date'] for b in used) if unit_kind == 'bet' \
            else Counter(d for (d, e) in groups.keys())
        tot_obs = tot_exp = 0; pcts = []
        for d in sorted(slate):
            exp = slate[d]; o = obs.get(d, 0)
            tot_obs += o; tot_exp += exp
            pcts.append(o / exp if exp else 0.0)
        cov_overall = tot_obs / tot_exp if tot_exp else 0.0
        cov_lo, cov_hi = min(pcts), max(pcts)
        cmin = strat.get('coverage_min')
        if cmin is not None and cov_overall < cmin:
            cov_ok = False

    conf = strat.get('significance')
    block = strat.get('block', 1)
    lb, pval = bootstrap(unit_pnls, conf, block=block)

    probed = [b for b in used if 'prob' in b]
    edge = None
    if probed:
        claimed_edge = sum(b['prob'] - b['price'] for b in probed) / len(probed)
        claimed_wr = sum(b['prob'] for b in probed) / len(probed)
        actual_wr = sum(1 for b in probed if b['won']) / len(probed)
        curve, ece = reliability(probed)
        edge = dict(m=len(probed), ce=claimed_edge, cw=claimed_wr, aw=actual_wr, ece=ece, curve=curve)

    r = dict(strat=strat, name=strat['name'], reg=reg, excluded=excluded, unit=unit_kind,
             nbets=len(used), n=n, total=total, per_unit=per_unit, win_rate=win_rate,
             avg_win=avg_win, avg_loss=avg_loss, cov_ok=cov_ok, cov_overall=cov_overall,
             cov_lo=cov_lo, cov_hi=cov_hi, conf=conf, block=block, lb=lb, pval=pval, edge=edge)

    reasons = []
    mu = strat.get('min_units')
    if mu is not None and n < mu:
        reasons.append(f"underpowered ({n} units < min_units {mu})")
    if not cov_ok:
        reasons.append(f"coverage {cov_overall:.0%} < required {strat['coverage_min']:.0%} (sample not representative)")
    r['no_verdict_reasons'] = reasons
    r['status'] = 'no_verdict' if reasons else 'eligible'
    return r


def bh_fdr(results, q):
    """Benjamini-Hochberg across all ELIGIBLE strategies' p-values. Marks each
    eligible result with r['discovery'] (survives FDR control at q)."""
    elig = [r for r in results if r['status'] == 'eligible']
    m = len(elig)
    ordered = sorted(elig, key=lambda r: r['pval'])
    cutoff_rank = 0
    for rank, r in enumerate(ordered, 1):
        if r['pval'] <= (rank / m) * q:
            cutoff_rank = rank
    disc = set(id(r) for r in ordered[:cutoff_rank])
    for r in elig:
        r['discovery'] = id(r) in disc
    return m, cutoff_rank


# ----------------------------------------------------------------- report ----
def _finalize_verdict(r, fdr):
    if r['status'] == 'no_verdict':
        return "NO VERDICT — " + "; ".join(r['no_verdict_reasons']) + \
               ". Not positive, not negative — not enough honest data to say."
    if r['per_unit'] <= 0:
        return "NO EDGE — per-unit EV <= 0. Vigil will not call this positive."
    if r['conf'] is not None and r['lb'] is not None and r['lb'] <= 0:
        return (f"NO EDGE — per-unit EV is +{r['per_unit']:.4f} but its {int(r['conf']*100)}% "
                f"lower bound is {r['lb']:+.4f} <= 0, not distinguishable from zero.")
    if fdr is not None and not r.get('discovery', False):
        return (f"NO EDGE — fails Benjamini-Hochberg FDR control at q={fdr:g} across the "
                f"{r['_family']} strategies in this file (p={r['pval']:.4f}). Looks positive "
                f"alone; not after correcting for how many strategies were tried.")
    return "POSITIVE (forward, per-unit EV > 0, significant, survives FDR)" if fdr is not None \
        else "POSITIVE (forward, per-unit EV > 0 and significant)"


def _print_result(r, fdr):
    s = r['strat']
    print(f"=== vigil report: {r['name']} ===")
    print(f"registered {r['reg']} | forward_only={bool(s.get('forward_only'))} "
          f"| unit={r['unit']} | pnl=canonical" + (f" | block={r['block']}" if r['block'] > 1 else ""))
    if r['excluded']:
        print(f"dropped {r['excluded']} in-sample bet(s) before {r['reg']}")
    print(f"bets {r['nbets']} -> units {r['n']}")
    print(f"P&L {r['total']:+.2f} | per-unit EV {r['per_unit']:+.4f} | unit win-rate {r['win_rate']:.0%}")
    print(f"avg win {r['avg_win']:+.3f} | avg loss {r['avg_loss']:+.3f}  (win-small / lose-big check)")
    if r['edge']:
        e = r['edge']
        tail = f" — model overconfident by {(e['cw']-e['aw'])*100:.0f} pts" if (e['cw']-e['aw'])*100 > 1 else ""
        print(f"model edge: claimed {e['ce']:+.3f}/bet on {e['m']} bets | "
              f"claimed win-rate {e['cw']:.0%} vs actual {e['aw']:.0%}{tail}")
        print(f"calibration: ECE {e['ece']:.3f} over {len(e['curve'])} bins (pred vs actual): " +
              ", ".join(f"{mp:.2f}->{wr:.2f}(n{c})" for mp, wr, c in e['curve']))
    if r['cov_overall'] is not None:
        tail = f" — below required {s['coverage_min']:.0%}" if not r['cov_ok'] else ""
        print(f"coverage {r['cov_overall']:.0%} of slate (per-day {r['cov_lo']:.0%}-{r['cov_hi']:.0%}){tail}")
    if r['lb'] is not None:
        print(f"per-unit EV {int(r['conf']*100)}% bootstrap lower bound {r['lb']:+.4f}"
              + (f" (block={r['block']})" if r['block'] > 1 else ""))
    if fdr is not None and r['status'] == 'eligible':
        print(f"FDR: p={r['pval']:.4f}  {'survives' if r.get('discovery') else 'FAILS'} "
              f"BH at q={fdr:g} across {r['_family']} strategies")
    print("verdict: " + _finalize_verdict(r, fdr))


def run(text, base_dir, lock_path=None):
    fdr, strategies = parse(text, base_dir)
    reported = [s for s in strategies if s['reported']]
    if not reported:
        raise VigilError("no `report <strategy>` statement")

    lock_note = None
    if lock_path and os.path.exists(lock_path):
        want = open(lock_path).read().strip().splitlines()[0].strip()
        if registration_hash(text) != want:
            raise VigilError("pre-registered protocol changed since it was locked — thresholds, "
                             "slate, or the set of strategies edited after pre-registration? that is "
                             "goalpost-moving. (delete the .lock only to start a NEW pre-registration.)")
        ots = lock_path + '.ots'
        lock_note = "locked & unchanged" + (" (OTS proof present)" if os.path.exists(ots) else
                                            " (no OTS proof — `vigil lock --ots` to anchor it in time)")
    elif lock_path:
        lock_note = "UNLOCKED (not pre-registered — run `vigil lock` before you look at data)"

    results = [evaluate(s) for s in reported]
    family = sum(1 for r in results if r['status'] == 'eligible')
    for r in results:
        r['_family'] = family
    if fdr is not None:
        bh_fdr(results, fdr)

    if lock_note:
        print(f"pre-registration: {lock_note}")
    if fdr is not None:
        print(f"FDR control: Benjamini-Hochberg at q={fdr:g} across {family} eligible strateg"
              f"{'y' if family == 1 else 'ies'} in this file.")
        print("  (FDR only controls the strategies IN this file — lock + timestamp the whole file "
              "so undeclared losers can't have been hidden.)")
    for idx, r in enumerate(results):
        if idx:
            print()
        _print_result(r, fdr)


# -------------------------------------------------------------- lock/verify --
def lock(path, do_ots=False):
    h = registration_hash(open(path).read())
    lp = path + '.lock'
    open(lp, 'w').write(h + '\n')
    print(f"locked {path} -> {lp} ({h[:16]}…)")
    print("this hash covers the whole pre-registered protocol: every strategy block, slate, and the")
    print("fdr setting — but NOT the bet data. commit the .lock alongside the .vig.")
    if do_ots:
        if not shutil.which('ots'):
            print("…but `ots` is not on PATH — install opentimestamps-client, then: ots stamp " + lp)
            return
        try:
            subprocess.run(['ots', 'stamp', lp], check=True)
            print(f"OpenTimestamps: wrote {lp}.ots — keep it; `vigil verify` and `ots verify` use it.")
        except subprocess.CalledProcessError as e:
            print(f"ots stamp failed ({e}); you can stamp manually: ots stamp {lp}")
    else:
        print(f"to anchor this hash in time: `vigil lock {path} --ots`  (or:  ots stamp {lp})")


def verify(path):
    lp = path + '.lock'
    if not os.path.exists(lp):
        raise VigilError(f"no lock file {lp} — run `vigil lock {path}` first")
    want = open(lp).read().strip().splitlines()[0].strip()
    have = registration_hash(open(path).read())
    if have != want:
        print(f"MISMATCH — protocol hash {have[:16]}… != locked {want[:16]}…")
        print("the pre-registered protocol changed since locking. this is the goalpost-moving guard.")
        sys.exit(1)
    print(f"OK — protocol matches the lock ({have[:16]}…)")
    ots = lp + '.ots'
    if not os.path.exists(ots):
        print(f"no OTS proof ({ots}). the hash above is in standard sha256 form for OTS-proofing:")
        print(f"  ots stamp {lp}    (anchors WHEN this protocol existed)")
        return
    if shutil.which('ots'):
        print(f"OTS proof present; verifying timestamp (needs a Bitcoin node / calendar access):")
        subprocess.run(['ots', 'verify', lp])
    else:
        print(f"OTS proof present ({ots}); `ots` not installed here. verify with: ots verify {lp}")


# ---------------------------------------------------------------- entry ------
def main(argv):
    if len(argv) >= 3 and argv[1] == 'lock':
        lock(argv[2], do_ots=('--ots' in argv[3:])); return
    if len(argv) == 3 and argv[1] == 'verify':
        try:
            verify(argv[2])
        except VigilError as e:
            print(f"vigil: error — {e}"); sys.exit(1)
        return
    if len(argv) == 3 and argv[1] == 'run':
        path = argv[2]
    elif len(argv) == 2:
        path = argv[1]
    else:
        print("usage: python3 vigil.py <file.vig> | run <file.vig> | "
              "lock <file.vig> [--ots] | verify <file.vig>")
        sys.exit(2)
    try:
        run(open(path).read(), os.path.dirname(os.path.abspath(path)), lock_path=path + '.lock')
    except VigilError as e:
        print(f"vigil: error — {e}")
        sys.exit(1)


if __name__ == '__main__':
    main(sys.argv)
