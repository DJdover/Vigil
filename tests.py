#!/usr/bin/env python3
"""Vigil test suite. Run: python3 tests.py"""
import io, os, contextlib, tempfile
import vigil

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def out(text):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        vigil.run(text, '.')
    return buf.getvalue()


def raises(text):
    try:
        out(text)
        return False
    except vigil.VigilError:
        return True


BASE = """strategy s {{
    registered 2026-01-08
    {opts}
    unit {unit}
    pnl canonical
}}
bets {{
{rows}
}}
report s
"""


def vig(rows, opts="forward_only", unit="event"):
    return BASE.format(opts=opts, unit=unit, rows="\n".join("    " + r for r in rows))


# 1. in-sample scoring is refused
check("in-sample data without forward_only is an error",
      raises(vig(["2026-01-07 | E1 | NO | 0.40 | true",
                  "2026-01-09 | E2 | YES | 0.60 | true"], opts="")))

# 2. pnl canonical is required
check("missing `pnl canonical` is an error",
      raises("strategy s {\n registered 2026-01-08\n forward_only\n unit bet\n}\n"
             "bets {\n 2026-01-09 | E1 | NO | 0.5 | true\n}\nreport s"))

# 3. price out of [0,1] is rejected
check("price outside [0,1] is an error",
      raises(vig(["2026-01-09 | E1 | NO | 1.40 | true"])))

# 4. correlated bets on one event collapse to one unit
o = out(vig(["2026-01-09 | E1 | NO | 0.31 | true",
             "2026-01-09 | E1 | NO | 0.50 | true",
             "2026-01-09 | E1 | NO | 0.82 | true"]))
check("three bets on one event -> 1 unit", "bets 3 -> units 1" in o)

# 5. min_units withholds the verdict
o = out(vig(["2026-01-09 | E1 | YES | 0.5 | true",
             "2026-01-10 | E2 | YES | 0.5 | true"],
            opts="forward_only\n    min_units 30"))
check("min_units below floor -> NO VERDICT", "NO VERDICT" in o and "underpowered" in o)

# 6. significance kills a positive-but-insignificant average
o = out(vig(["2026-01-09 | E1 | YES | 0.5 | true",
             "2026-01-10 | E2 | YES | 0.5 | true",
             "2026-01-11 | E3 | YES | 0.5 | true",
             "2026-01-12 | E4 | YES | 0.5 | true",
             "2026-01-13 | E5 | YES | 0.5 | false",
             "2026-01-14 | E6 | YES | 0.5 | false"],
            opts="forward_only\n    significance 0.95", unit="bet"))
check("positive average, insignificant -> NO EDGE",
      "NO EDGE" in o and "lower bound" in o)

# 7. model edge / overconfidence is reported when prob is supplied
o = out(vig(["2026-01-09 | E1 | YES | 0.50 | false | 0.80",
             "2026-01-10 | E2 | YES | 0.50 | false | 0.80",
             "2026-01-11 | E3 | YES | 0.50 | true  | 0.80"], unit="bet"))
check("model edge line appears with prob", "model edge:" in o)
check("overconfident model is flagged", "overconfident" in o)

# 8. pre-registration lock detects a changed strategy block
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "s.vig")
    open(p, "w").write(vig(["2026-01-09 | E1 | YES | 0.5 | true"],
                           opts="forward_only\n    min_units 30"))
    vigil.lock(p)
    # unchanged -> runs fine (no raise)
    try:
        vigil.run(open(p).read(), d, lock_path=p + ".lock")
        unchanged_ok = True
    except vigil.VigilError:
        unchanged_ok = False
    check("locked + unchanged runs", unchanged_ok)
    # tamper with a threshold, then it must be caught
    open(p, "w").write(vig(["2026-01-09 | E1 | YES | 0.5 | true"],
                           opts="forward_only\n    min_units 1"))
    tampered_caught = False
    try:
        vigil.run(open(p).read(), d, lock_path=p + ".lock")
    except vigil.VigilError as e:
        tampered_caught = "changed since it was locked" in str(e)
    check("locked + tampered threshold is caught", tampered_caught)

# 9. non-ISO dates are rejected (no more lexical comparison)
check("non-ISO bet date is an error",
      raises(vig(["2026/01/09 | E1 | NO | 0.40 | true"])))

# 10. block bootstrap field parses and is reported
o = out(vig(["2026-01-%02d | E%d | YES | 0.5 | %s" % (9 + i, i, "true" if i % 2 else "false")
             for i in range(1, 9)],
            opts="forward_only\n    significance 0.95\n    block 2", unit="bet"))
check("block bootstrap parses and is labeled", "block=2" in o)

# 11. ECE / reliability line appears with prob
o = out(vig(["2026-01-09 | E1 | YES | 0.50 | false | 0.80",
             "2026-01-10 | E2 | YES | 0.50 | true  | 0.80",
             "2026-01-11 | E3 | YES | 0.50 | false | 0.20"], unit="bet"))
check("calibration/ECE line appears", "calibration: ECE" in o)

# 12. BH-FDR across a batch: strong survives, modest-but-multiplicity fails
def rows_win(n, price):
    return ["2026-02-15 | W%d | YES | %.2f | true" % (i, price) for i in range(n)]
def rows_mix(nwin, nloss, price):
    r = ["2026-03-15 | M%d | YES | %.2f | true" % (i, price) for i in range(nwin)]
    r += ["2026-04-15 | L%d | YES | %.2f | false" % (i, price) for i in range(nloss)]
    return r
batch = ("fdr 0.05\n"
         "strategy strong {\n registered 2026-01-01\n forward_only\n unit bet\n pnl canonical\n min_units 30\n}\n"
         "bets {\n" + "\n".join(" " + r for r in rows_win(40, 0.20)) + "\n}\nreport strong\n"
         "strategy modest {\n registered 2026-01-01\n forward_only\n unit bet\n pnl canonical\n min_units 30\n}\n"
         "bets {\n" + "\n".join(" " + r for r in rows_mix(21, 19, 0.50)) + "\n}\nreport modest\n")
o = out(batch)
check("FDR control is announced for a batch", "FDR control: Benjamini-Hochberg" in o)
check("strong strategy survives FDR -> POSITIVE", "POSITIVE" in o.split("modest")[0])
check("modest strategy fails FDR multiplicity", "fails Benjamini-Hochberg" in o)

# 13. the lock covers the WHOLE batch: adding a strategy changes the registration hash
h1 = vigil.registration_hash(batch)
batch2 = batch + ("strategy extra {\n registered 2026-01-01\n forward_only\n unit bet\n pnl canonical\n}\n"
                  "bets {\n 2026-05-01 | X1 | YES | 0.5 | true\n}\nreport extra\n")
check("adding a strategy changes the registration hash", vigil.registration_hash(batch2) != h1)

# 14. the lock covers the slate too (can't lower the slate after the fact)
sl = lambda exp: ("strategy s {\n registered 2026-01-08\n forward_only\n unit event\n pnl canonical\n"
                  " coverage min 0.6\n}\nslate {\n 2026-01-09 | %d\n}\n"
                  "bets {\n 2026-01-09 | E1 | YES | 0.5 | true\n}\nreport s" % exp)
check("changing the slate changes the registration hash",
      vigil.registration_hash(sl(20)) != vigil.registration_hash(sl(5)))

# 15. verify: OK on match, non-zero exit on mismatch
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "v.vig")
    open(p, "w").write(vig(["2026-01-09 | E1 | YES | 0.5 | true"], opts="forward_only\n    min_units 30"))
    vigil.lock(p)
    ok = True
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            vigil.verify(p)
    except SystemExit:
        ok = False
    check("verify passes on an unchanged protocol", ok)
    open(p, "w").write(vig(["2026-01-09 | E1 | YES | 0.5 | true"], opts="forward_only\n    min_units 1"))
    caught = False
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            vigil.verify(p)
    except SystemExit:
        caught = True
    check("verify fails (non-zero) on a tampered protocol", caught)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
