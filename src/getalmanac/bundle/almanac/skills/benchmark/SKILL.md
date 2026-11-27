---
name: benchmark
description: Measure the speed of a Python project before and after a change, with the standard library only, and flag regressions against a saved baseline. Use when checking whether a change made code slower, when someone says it feels slow, or before a release that has speed targets.
---

# Benchmark

Measure before you change anything. A number without a baseline proves nothing.

## Steps

1. **Pick the target.** One function, one script, or the test suite. Write down
   the exact input, the machine, the Python version, and whether the run is
   warm (after one throwaway run) or cold (fresh process).
2. **Measure the baseline first** and record it (for example `benchmarks/baseline.json`)
   and hand it to the lead, who decides whether to commit it.
3. **Time it** with the standard library:
   - One function: `timeit.repeat(...)`, or `time.perf_counter()` around the call.
   - A whole script: `time.perf_counter()` around `subprocess.run([...])`.
   - The test suite: `time.perf_counter()` around `python -m unittest`.
   - Where the time goes: `python -m cProfile -s cumtime script.py`.
4. **Repeat.** Run at least 5 times and report the median, not the mean or the best.
5. **Compare like with like.** Same input, same machine, same warm or cold state,
   same Python. If one of these differs, the comparison is void: rerun.
6. **Flag regressions.** A change is a regression when its median is slower than
   the baseline by more than the noise. Measure the noise: the spread of the
   baseline runs, usually a few percent.

Example timing helper:

```python
import statistics
import timeit

times = timeit.repeat("parse(data)", globals=globals(), number=100, repeat=7)
print(f"{statistics.median(times) / 100 * 1000:.2f} ms per call")
```

## Report

Give a small table, then the verdict:

| Target | Before (median) | After (median) | Delta | Verdict |
|--------|-----------------|----------------|-------|---------|
| `parse(data)`, 10k rows | 41.2 ms | 47.9 ms | +16% | REGRESSION |
| `python -m unittest` | 3.10 s | 2.95 s | -5% | within noise |

Under the table, state:
- Input, machine, Python version, warm or cold, and the number of runs.
- The noise you measured.
- What you did not measure.

Changes no code. Fixing a regression is a separate step.
