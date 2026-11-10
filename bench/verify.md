# VERIFY: reproducing the README benchmark numbers

Session date: 2026-11-10. Checkout: `master` at `4ee71dd` ("phase gpu"), clean
worktree before this pass. Nothing under `src/`, `python/`, or `tests/` was read
as trusted until re-measured. This file records what was reproduced, what was
not, and what was changed.

## Gate

```
pixi run build && pixi run test   # 413 passed, exit 0
pixi run bench                    # exit 0
```

Run one after another as required, then two more standalone `pixi run bench`
runs to measure spread. Machine: Intel Xeon E5-2697 v4, Linux 6.8.0-147-generic,
Python 3.13.14. The host is shared: `/proc/loadavg` was 30-33 throughout
(72 logical CPUs).

## Headline table

Method: three full `pixi run bench` runs; every row below is the run whose
values sit at the median of the three per column. All three runs are within
20% of each other on every row. The old README values were not reproducible
outside 20% for rows 3 and 4 (understated `get` hit cost and understated the
upstream hit cost), so the README table was replaced with these.

| case | old README | run 1 | run 2 | run 3 | new README (median run) |
| --- | --- | --- | --- | --- | --- |
| scalar `get`, 50k miss-heavy | 126.47 / 252.91 | 121.46 / 257.50 | 117.75 / 249.22 | 137.59 / 258.03 | 121.46 / 257.50 (2.12x) |
| batch `get_many`, 50k miss-heavy | 31.72 / 233.64 | 36.18 / 256.96 | 34.22 / 249.23 | 34.98 / 254.00 | 34.22 / 254.00 (7.28x) |
| scalar `get`, 10k GeoIP2 hits | 180.99 / 1668.40 | 193.18 / 1704.44 | 184.94 / 1773.75 | 190.91 / 1662.07 | 190.91 / 1735.15 (9.59x) |
| batch `get_many`, 10k GeoIP2 hits | 172.36 / 1668.87 | 179.38 / 1772.94 | 175.84 / 1714.33 | 184.29 / 1786.86 | 179.38 / 1772.94 (9.88x) |
| batch get + prefix, 50k miss-heavy | 31.15 / 255.99 | 33.85 / 273.51 | 31.60 / 262.40 | 32.71 / 253.26 | 32.71 / 262.40 (8.08x) |

(mojo / upstream, ms.)

No row is a regression: the port is faster than upstream on all five, so no
"slower than upstream" row had to be preserved in this table. They remain
slower in no row.

## Same-input / no-skipped-work audit of `bench/bench.py`

- Both sides open the same file (`tests/data/GeoIP2-City-Test.mmdb`) in
  `MODE_MMAP`, and both use the same address lists: 50k random misses and 10k
  known hits are built once and passed to both lambdas.
- The port is imported from `python/` and upstream from site-packages; checked
  in-session that the two resolve to different files and that the port's
  `_lib.lib()` is loaded. `load_upstream()` swaps `sys.modules` so the bench
  cannot accidentally time the port against itself.
- `get_many` is not a shortcut: `Reader.get_many` calls
  `get_with_prefix_len_many`, whose IPv4 path decodes every pointer through
  `_resolve_data_pointer` and builds one list entry per address. The traversal
  is batched; decode, clone, and list building are not skipped. The upstream
  side runs the scalar `get` loop, since upstream has no batch API.
- `bench.py` asserts `actual == expected` (port vs upstream results) for every
  case before timing, so a case that skipped decode would fail parity first.
- Timings are best-of-three whole calls; the result value is checked to be
  non-`None`.

## Sub-tables in the README

| claim | status this session |
| --- | --- |
| SIMD phase, "before" column (old `src/maxminddb.mojo`) | **Not reproducible.** "Before" is documented as `HEAD`'s source, but the working tree and `HEAD` have identical `src/` (`git diff HEAD` empty), so `HEAD` IS this tree. Built `HEAD` in a separate worktree into `/tmp/mmdb-libs/head.so` and A/B'd it against `dist/libmojo-maxminddb.so` with `bench/_api_ab.py`: `get_many` 50k misses 1.02x / 1.03x, 10k hits 0.98x / 0.99x (swapped order), i.e. identical. The older commit `7f5704e` does not compile on the pinned Mojo 1.2.0 toolchain, so the pre-SIMD baseline cannot be rebuilt. Those "before" numbers were left untouched (they are not headline claims and cannot be independently regenerated from this checkout); treat them as historical. |
| SIMD phase, "after" kernel row | **Reproduced.** `bench/_kernel_ab.py` with this tree's library gives 1.85-1.98 ms for 50k misses at the kernel and 0.99-1.03 ms for 10k hits; README's after column is 1.98 ms and 1.03 ms. |
| Parallel crossover gate (50k / 262,144 / 300,000 addresses) | **Reproduced in shape and order of magnitude.** Direct `mmd_find_ipv4_many` calls, 9 repeats x 3 runs: 262,143 addresses 10.12-11.81 ms (serial), 262,144 7.13-8.29 ms (4 workers), 300,000 7.56-11.05 ms. README: 10.30, 7.92, 8.83 ms. The step at the 2^18 threshold is present; absolute values move with host load. Not edited. |
| Parallel "pixi run bench is unchanged" paragraph (35.27 ms / 136.51 ms) | Same order as this session's 34.22 / 121.46-137.59 ms; left as written (it is explicitly a spread-tolerant observation, not a table). |

## Bench coverage gaps found

- The five benchmark rows are all homogeneous IPv4. The mixed IPv4/IPv6
  `get_with_prefix_len_many` path is unbenchmarked, and `bench/analysis.md`
  row 8 called it "the only measured regression against upstream". Measured
  this session on 10k known hits alternating IPv4/IPv6 addresses (parity
  asserted first): port 179.99 ms vs upstream 1732.46 ms scalar
  `get_with_prefix_len` loop, i.e. 9.63x faster, not a regression. The
  analysis.md figure appears to reflect the older per-element mixed path.
  No README number was taken from this measurement; it is recorded here so the
  contradiction is on the record rather than silently resolved.

## Tests

`git log -p -- tests/` was read in full for deletions.

- Only six lines were ever removed from `tests/`, all in parametrize lists:
  `count=[16383, 16384]` -> `[262_143, 262_144, 262_145]` (threshold moved),
  `count=[1, 3, 5, 9, 17]` -> `[0, 1, 2, 3, 4, 5, 9, 17, 1001]` (widened), and
  the session-scoped `upstream_maxminddb` fixture refactored into
  `_load_upstream()` plus fixtures (same import logic, more fixtures). No
  assertion or test function was deleted.
- Tolerances (`FLOAT32_ABS_TOL = 1e-3`, `DOUBLE_ABS_TOL = 0.0`) were introduced
  in commit `274dd93` as additions; `git log -p` shows no tolerance widening.
  They match upstream's own `places=3` float comparison and exact double
  comparison.
- No `skip`/`xfail` markers exist in tests; the only `pytest.skip` is the
  fixture's "upstream maxminddb is not installed" guard, which does not fire
  here (upstream is installed; parity tests executed).
- Every parity test compares against the installed upstream 2.6.2 package or
  upstream's published decoder vectors. No restoration was needed.

## Code changes

None. No failing test was produced, so nothing under `src/`, `python/`, or
`tests/` was modified.

## Files changed

- `README.md`: headline benchmark table replaced with this session's medians.
- `bench/verify.md`: this file.
