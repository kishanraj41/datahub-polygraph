# Upstream PR draft — AutoLineage

**Status: DRAFT. Not opened.** Review before submitting to
`github.com/kishanraj41/autolineage`.

Patch: [`autolineage-pathlib-fix.patch`](autolineage-pathlib-fix.patch)

---

## Title

fix(hooks): capture file lineage when paths are `pathlib.Path`

## Problem

`PandasIOHooks` tests `isinstance(filepath_or_buffer, str)` before recording a
read, and `isinstance(args[0], str)` before recording a write. `pathlib.Path` is
not a `str`, so:

```python
from pathlib import Path
import autolineage.auto
import pandas as pd

df = pd.read_csv(Path("data/raw.csv"))   # captured: nothing
df.to_csv(Path("out/result.csv"))        # captured: nothing
```

No exception, no warning. The tracker still records every downstream pandas and
sklearn operation, so the run looks fully instrumented — the graph just has no
file anchors, and every node's `filepath` is `None`.

That is the worst shape for a bug in a lineage tool: it fails silently and the
output looks plausible. `pathlib` is the idiomatic way to handle paths in modern
Python, so this is likely to affect real users rather than being an edge case.

I hit it building [Polygraph](https://github.com/kishanraj41/datahub-polygraph),
which reconciles AutoLineage captures against DataHub's declared lineage. The
reconciliation came back with zero mapped assets and I assumed my own code was
wrong for some time before checking the trace and finding every `filepath` null.

## Fix

Accept `os.PathLike` alongside `str` and normalise with `os.fspath`. Four call
sites, no behaviour change for existing `str` callers.

`os.path.exists` and `os.path.abspath` both already accept `PathLike`, so only
the `isinstance` guards and the stored value needed changing.

## Test

```python
def test_pathlib_paths_are_captured(tmp_path):
    """pathlib.Path arguments must produce file lineage, not silence."""
    import autolineage.auto
    from autolineage.auto import get_tracker
    import pandas as pd

    src = tmp_path / "in.csv"
    pd.DataFrame({"a": [1, 2, 3]}).to_csv(str(src), index=False)

    df = pd.read_csv(src)              # Path, not str
    out = tmp_path / "out.csv"
    df.to_csv(out)                     # Path, not str

    tracker = get_tracker()
    io_records = [r for r in tracker.records if r.category == "io"]
    captured = {r.metadata.get("filepath") for r in io_records}

    assert str(src.resolve()) in captured, "read_csv(Path) produced no lineage"
    assert str(out.resolve()) in captured, "to_csv(Path) produced no lineage"

    anchors = [n for n in tracker.nodes.values() if n.filepath]
    assert anchors, "no node carries a filepath; the graph has no file anchors"
```

## Scope

- Only `pandas_io.py`. `pyspark_hooks.py` takes paths as strings through the
  Spark API and is unaffected.
- No change to the record schema or the public API.
- Existing `str` callers are unaffected — `os.fspath("x")` returns `"x"`.

## Suggested changelog entry

```
### Fixed
- File lineage is now captured when `pathlib.Path` objects are passed to
  pandas read/write functions. Previously these were silently skipped, producing
  a trace with no file anchors and no error.
```
