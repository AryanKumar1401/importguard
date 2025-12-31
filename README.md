# importguard

**Measure and enforce import-time behavior in Python projects.**

Stop slow imports and sneaky side effects from shipping to production.

---

## The Problem

Python imports can quietly become a performance and reliability trap:

- **Slow startup** — Your CLI or server cold-start gets slower because importing your package pulls in heavy modules (`pandas`, `torch`, `boto3`) or runs expensive initialization
- **Hidden side effects** — Importing a module accidentally reads config, makes network calls, hits the filesystem, or initializes logging globally
- **Hard to catch in review** — These regressions are subtle and often only noticed when users complain

**importguard** makes these issues measurable and CI-enforceable.

---

## Installation

```bash
pip install importguard
```

---

## Quick Start

### Check import time for any module

```bash
$ importguard check requests

✓ requests imported in 45ms

Top 5 slowest imports:
  1. urllib3.util.ssl_     12ms
  2. urllib3.util          8ms
  3. requests.adapters     7ms
  4. charset_normalizer    6ms
  5. requests.models       5ms
```

### Enforce a time budget

```bash
$ importguard check mypkg --max-ms 200

✓ mypkg imported in 127ms (budget: 200ms)
```

```bash
$ importguard check mypkg --max-ms 100

✗ FAIL: mypkg imported in 127ms (budget: 100ms)
```

### Ban imports at the top level

```bash
$ importguard check mypkg.cli --ban pandas --ban torch

✗ FAIL: mypkg.cli imports banned module: pandas
```

### Get reliable timing with --repeat

Import timing can be noisy. Use `--repeat` to run multiple times and report the median:

```bash
$ importguard check mypkg --max-ms 150 --repeat 5

Running 5 iterations...
✓ mypkg imported in 127ms (median of 5 runs: 118ms, 127ms, 132ms, 124ms, 129ms)
```

This is especially useful in CI where timing variance can cause flaky failures.

### Pin Python interpreter with --python

Useful for testing against specific Python versions or when using pyenv/virtualenvs in CI:

```bash
$ importguard check mypkg --python /usr/local/bin/python3.11 --max-ms 200

✓ mypkg imported in 134ms (using Python 3.11.7)
```

---

## Configuration

Create `.importguard.toml` in your project root:

```toml
[importguard]
# Global budget for the main package
max_total_ms = 200

# Per-module budgets
[importguard.budgets]
"mypkg" = 150
"mypkg.cli" = 100
"mypkg.api" = 80

# Banned top-level imports per module
[importguard.banned]
"mypkg.cli" = ["pandas", "numpy", "torch"]
"mypkg" = ["boto3", "tensorflow"]
```

Then run:

```bash
$ importguard check mypkg --config .importguard.toml
```

---

## CLI Reference

```
importguard check <module> [options]

Arguments:
  module              Python module to check (e.g., mypkg, mypkg.cli)

Options:
  --max-ms MS         Fail if import exceeds this threshold
  --ban MODULE        Ban a module from being imported (can repeat)
  --config FILE       Path to .importguard.toml config file
  --top N             Show top N slowest imports (default: 10)
  --json              Output results as JSON (for CI parsing)
  --quiet             Only output on failure
  --fail-on-warning   Exit non-zero on warnings, not just errors
  --repeat K          Run K times in fresh subprocesses, report median (reduces noise)
  --python PATH       Use specific Python interpreter (e.g., /usr/bin/python3.11)
```

---

## CI Integration

### GitHub Actions

```yaml
name: Import Guard

on: [push, pull_request]

jobs:
  import-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: |
          pip install -e .
          pip install importguard
      
      - name: Check import performance
        run: |
          importguard check mypkg --config .importguard.toml --fail-on-warning --repeat 3
```

**CI Best Practices:**
- Use `--repeat 3` or `--repeat 5` to reduce timing noise and prevent flaky failures
- Use `--python $(which python)` to explicitly control which interpreter is tested
- Each repeat runs in a fresh subprocess to avoid caching effects

### Pre-commit Hook

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: importguard
        name: importguard
        entry: importguard check mypkg --max-ms 200
        language: system
        pass_filenames: false
        always_run: true
```

---

## Why Results Differ on CI?

Import times can vary significantly between your local machine and CI. Here's why and how to handle it:

### Common Causes

| Factor | Local | CI | Impact |
|--------|-------|-----|--------|
| **CPU** | Fast desktop/laptop | Shared VM cores | 2-5x slower |
| **Disk** | SSD/NVMe | Network storage | Variable latency |
| **Caching** | Warm `.pyc` files | Cold start | First run slower |
| **Python version** | May differ | Matrix testing | Different stdlib |
| **Dependencies** | Pinned versions | Fresh install | Version variance |

### Recommendations

1. **Use `--repeat 3` or `--repeat 5`** — Takes median to smooth out noise
2. **Set CI budgets 2-3x higher** than local measurements
3. **Use separate config sections** for CI vs local:

```toml
# .importguard.toml
[importguard]
max_total_ms = 200  # Local development

# Override in CI with environment-specific config
# or use: importguard check mypkg --max-ms 500
```

4. **Pin Python version** in CI to match local:

```yaml
- uses: actions/setup-python@v5
  with:
    python-version: '3.11.7'  # Exact version
```

5. **Warm up before measuring** (optional):

```yaml
- name: Warm up Python cache
  run: python -c "import mypkg" || true

- name: Check import performance  
  run: importguard check mypkg --repeat 3
```

### Debugging CI Failures

```bash
# Get detailed JSON output for investigation
importguard check mypkg --json > import-report.json

# Compare top imports between local and CI
importguard check mypkg --top 20
```

---

## How to Keep Imports Fast

### 1. Lazy Imports

Move heavy imports inside functions that need them:

```python
# ❌ Bad: imports pandas at module load
import pandas as pd

def process_data(path):
    return pd.read_csv(path)

# ✅ Good: imports pandas only when needed
def process_data(path):
    import pandas as pd
    return pd.read_csv(path)
```

### 2. Optional Dependencies

Use try/except for optional heavy dependencies:

```python
# ✅ Good: graceful degradation
try:
    import torch
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

def train_model(data):
    if not HAS_TORCH:
        raise ImportError("torch required: pip install mypkg[ml]")
    # ... use torch
```

### 3. Deferred Imports with `__getattr__`

For library authors, use module-level `__getattr__`:

```python
# mypkg/__init__.py
def __getattr__(name):
    if name == "heavy_module":
        from . import heavy_module
        return heavy_module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

### 4. Split Entry Points

Separate your CLI from your library:

```
mypkg/
├── __init__.py      # Lightweight, fast to import
├── core.py          # Core functionality
├── cli.py           # CLI-only, can import Click/Rich
└── optional/
    └── ml.py        # Heavy ML dependencies
```

```toml
# pyproject.toml
[project.scripts]
mypkg = "mypkg.cli:main"  # CLI imports heavy deps
```

### 5. Avoid Import Side Effects

```python
# ❌ Bad: runs on import
config = load_config()  # Network call!
logger = setup_logging()  # Creates files!

# ✅ Good: explicit initialization
_config = None
def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config
```

### 6. Profile Before Optimizing

Use importguard to find the actual culprits:

```bash
# Find the slowest imports
importguard check mypkg --top 20

# Ban known-heavy modules from your fast path
importguard check mypkg.cli --ban pandas --ban torch --ban tensorflow
```

---

## Python API

```python
from importguard import check_import, ImportResult

# Basic timing
result: ImportResult = check_import("mypkg")
print(f"Import took {result.total_ms:.1f}ms")

# Check against rules
result = check_import(
    "mypkg.cli",
    max_ms=100,
    banned=["pandas", "torch"]
)

if not result.passed:
    for violation in result.violations:
        print(f"FAIL: {violation}")

# Reduce noise with repeated measurements
result = check_import(
    "mypkg",
    repeat=5,  # Run 5 times, report median
    max_ms=150
)
print(f"Median: {result.median_ms:.1f}ms across {result.num_runs} runs")

# Use specific Python interpreter
result = check_import(
    "mypkg",
    python_path="/usr/local/bin/python3.11"
)
```

---

## How It Works

importguard uses Python's `-X importtime` flag to capture detailed timing data for every module imported during the load of your target module. It then:

1. Parses the import tree and timing data
2. Identifies the slowest imports
3. Checks for banned modules in the import chain
4. Compares against your configured budgets
5. Reports violations with actionable output

All checks run in an isolated subprocess to avoid polluting your current environment.

---

## Use Cases

### CLI Startup Time
Keep your CLI snappy. Users notice when `mycli --help` takes 2 seconds.

### Library Hygiene  
Ensure `import mypkg` doesn't force users to install or load optional heavy dependencies.

### Serverless Cold Starts
Lambda and Cloud Functions cold starts are dominated by import time. Catch regressions before they hit prod.

### Monorepo Guardrails
Prevent accidental cross-module imports that pull in the kitchen sink.

---

## Roadmap

- [x] Import timing measurement
- [x] Time budget enforcement
- [x] Banned import detection
- [ ] Baseline comparison (diff vs main branch)
- [ ] Side-effect detection (network calls, file writes during import)
- [ ] HTML report generation
- [ ] VS Code extension

---

## Contributing

Contributions welcome! Please open an issue first to discuss what you'd like to change.

```bash
git clone https://github.com/AryanKumar1401/importguard
cd importguard
pip install -e ".[dev]"
pytest
```

---

## License

MIT

---

<p align="center">
  <strong>importguard</strong> — unit tests, but for import behavior
</p>

