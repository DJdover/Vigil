# Changelog

## v0.2

- **False-discovery-rate control across a batch.** Multiple strategies per file plus
  a file-level `fdr Q` apply a Benjamini-Hochberg correction over the whole batch, so
  a strategy that looks positive alone is refused if it doesn't survive correction for
  how many were tried. Defeats the "we ran fifty, one looked good" trap. See
  `examples/demo_batch.vig`.
- **OTS-backed integrity.** `vigil lock` now hashes the *whole pre-registered
  protocol* — every strategy block, every slate, and the `fdr` setting (never the bet
  data). `vigil lock --ots` anchors that hash via OpenTimestamps; new `vigil verify`
  re-checks the hash and the timestamp. Locking the whole file is what makes the FDR
  honest: the timestamp proves you didn't hide the losers.
- **Moving-block bootstrap.** `block K` resamples K consecutive units, so
  serially-correlated runs don't masquerade as independent evidence.
- **Real ISO date parsing.** Dates are parsed as dates (not compared lexically); a
  non-ISO date is now an error instead of silently misbehaving.
- **Calibration check.** With a `prob` column, the report adds a reliability curve and
  ECE alongside the claimed-vs-actual edge and the overconfidence flag.
- Honest-limits section in the README: Vigil verifies *internal* honesty; inputs
  (registration date, slate, data) are self-attested, and the lock + OTS close that
  gap as far as any tool can — but cannot make your data true.

## v0.1

- Initial release: `forward_only`, `unit`, `pnl canonical`, `min_units`, `coverage`,
  `significance` (by-unit bootstrap lower bound), win-rate-never-without-EV reporting,
  and a hash lock over the strategy block.
