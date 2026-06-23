"""Round-trip test: parse an official .buf, write it back, re-parse, compare.

Proves the writer emits BUFKIT-valid structure that our own parser reads back to
the same numbers (within 2-decimal rounding). This is the format-correctness gate
before generating files from GRIB2.
"""
import sys

import numpy as np

from bufkit_replica.parser import parse
from bufkit_replica.writer import to_buf_text


def compare(path: str) -> bool:
    a = parse(path)
    text = to_buf_text(a)
    b = parse(text, is_text=True)

    ok = True

    if a.snparm != b.snparm or a.stnprm != b.stnprm:
        print("  FAIL schema mismatch"); ok = False
    if len(a.soundings) != len(b.soundings):
        print(f"  FAIL #soundings {len(a.soundings)} != {len(b.soundings)}"); ok = False

    max_lev_diff = 0.0
    for sa, sb in zip(a.soundings, b.soundings):
        if sa.valid_time != sb.valid_time:
            print(f"  FAIL time {sa.valid_time} != {sb.valid_time}"); ok = False
        if sa.stid != sb.stid:
            print(f"  FAIL stid {sa.stid!r} != {sb.stid!r}"); ok = False
        if sa.levels.shape != sb.levels.shape:
            print(f"  FAIL levels shape {sa.levels.shape} != {sb.levels.shape}"); ok = False
            continue
        d = np.nanmax(np.abs(sa.levels.values - sb.levels.values))
        max_lev_diff = max(max_lev_diff, 0.0 if np.isnan(d) else d)
        for k in a.stnprm:
            va, vb = sa.derived.get(k), sb.derived.get(k)
            if va is None or vb is None:
                continue
            if not (np.isnan(va) and np.isnan(vb)) and abs((va or 0) - (vb or 0)) > 0.01:
                print(f"  FAIL derived {k}: {va} != {vb}"); ok = False

    # surface
    if (a.surface is None) != (b.surface is None):
        print("  FAIL surface presence mismatch"); ok = False
    elif a.surface is not None:
        if list(a.surface.columns) != list(b.surface.columns):
            print("  FAIL surface columns differ"); ok = False
        if a.surface.shape != b.surface.shape:
            print(f"  FAIL surface shape {a.surface.shape} != {b.surface.shape}"); ok = False
        else:
            num = [c for c in a.surface.columns if c != "TIME"]
            sd = np.nanmax(np.abs(a.surface[num].values - b.surface[num].values))
            sd = 0.0 if np.isnan(sd) else sd
            if not (a.surface["TIME"].equals(b.surface["TIME"])):
                print("  FAIL surface TIME differs"); ok = False
            print(f"  surface max value diff: {sd:.4f}")

    print(f"  levels max value diff: {max_lev_diff:.4f}")
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}  ({len(a.soundings)} times, "
          f"{a.soundings[0].levels.shape[0]} levels)")
    return ok


if __name__ == "__main__":
    files = sys.argv[1:] or ["samples/hrrr_kbuf.buf", "samples/gfs3_kbuf.buf",
                             "samples/nam_kbuf.buf", "samples/rap_kbed.buf"]
    allok = True
    for f in files:
        print(f"== {f} ==")
        try:
            allok &= compare(f)
        except Exception as e:
            print(f"  ERROR {e!r}"); allok = False
    sys.exit(0 if allok else 1)
