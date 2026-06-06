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

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
