"""
BUFKIT .buf writer - the inverse of parser.py.

Emits a BUFKIT-format ASCII file from structured data (soundings + surface series)
so the output can be dropped into a BUFKIT Data folder and opened by the real
program. Layout matches the official PSU/NCEP files:

  <blank>
  SNPARM = ...
  STNPRM = ...
  <blank>
  [per forecast time:]
    STID = KBED STNM = 744900 TIME = 260607/1600
    SLAT = .. SLON = .. SELV = ..
    STIM = 0
    <blank>
    <derived params, 4 "NAME = value" per line, STNPRM order>
    <blank>
    <SNPARM column names, 8 per line>
    <per level: SNPARM values, 8 per line>
    <blank>
  STN YYMMDD/HHMM <surface param names, 6 per line after the first>
  [per forecast time:]
    <STN TIME + surface values, 6 per line after the first>
    <blank>

Values use 2-decimal fixed point; missing = -9999.00. BUFKIT parses by whitespace
so exact decimals don't matter for readability - this format is BUFKIT-valid.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

MISSING = -9999.00


def _fmt(v) -> str:
    if v is None:
        return "-9999.00"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "-9999.00"
    if math.isnan(f):
        return "-9999.00"
    return f"{f:.2f}"


def _chunk(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def to_buf_text(bf, *, surface_param_order=None) -> str:
    """Render a BufkitFile-like object to BUFKIT .buf text.

    `bf` needs: .snparm (list), .stnprm (list), .soundings (list of Sounding),
    .surface (DataFrame with columns STN, TIME, then surface params).
    """
    lines: list[str] = [""]
    lines.append("SNPARM = " + ";".join(bf.snparm))
    lines.append("STNPRM = " + ";".join(bf.stnprm))
    lines.append("")

    # ---- per-time upper-air blocks ----
    for stim_idx, snd in enumerate(bf.soundings):
        tstr = snd.valid_time.strftime("%y%m%d/%H%M") if snd.valid_time is not None else "-9999"
        try:
            stnm_i = int(snd.stnm) if (snd.stnm is not None and not (isinstance(snd.stnm, float) and math.isnan(snd.stnm))) else 0
        except (TypeError, ValueError):
            stnm_i = 0
        lines.append(f"STID = {snd.stid} STNM = {stnm_i} TIME = {tstr}")
        slat = float(snd.slat) if snd.slat is not None else 0.0
        slon = float(snd.slon) if snd.slon is not None else 0.0
        selv = float(snd.selv) if snd.selv is not None else 0.0
        # Official BUFKIT files write SELV as integer (no decimal); SLAT/SLON keep decimals
        lines.append(f"SLAT = {slat:.2f} SLON = {slon:.2f} SELV = {int(round(selv))}")
        lines.append(f"STIM = {stim_idx}")
        lines.append("")

        # derived params, 4 "NAME = value" pairs per line, in STNPRM order
        pairs = [f"{name} = {_fmt(snd.derived.get(name))}" for name in bf.stnprm]
        for grp in _chunk(pairs, 4):
            lines.append(" ".join(grp))
        lines.append("")

        # SNPARM column-name header, 8 per line
        for grp in _chunk(bf.snparm, 8):
            lines.append(" ".join(grp))

        # per-level values, 8 per line
        for _, row in snd.levels.iterrows():
            vals = [_fmt(row[c]) for c in bf.snparm]
            for grp in _chunk(vals, 8):
                lines.append(" ".join(grp))
        lines.append("")

    # drop the trailing blank so the surface header follows the last profile directly
    if lines and lines[-1] == "":
        lines.pop()

    # ---- surface time-series section ----
    if bf.surface is not None and len(bf.surface) > 0:
        sfc = bf.surface
        params = surface_param_order or [c for c in sfc.columns if c not in ("STN", "TIME")]

        # header: "STN YYMMDD/HHMM" + first 6 params, then 6 per line
        header_tokens = ["STN", "YYMMDD/HHMM"] + params
        first = header_tokens[:8]
        lines.append(" ".join(first))
        for grp in _chunk(header_tokens[8:], 6):
            lines.append(" ".join(grp))

        # data rows: NO blank lines between records; two blank lines at the very end
        for _, row in sfc.iterrows():
            try:
                stn = int(row["STN"]) if (row["STN"] is not None and not pd.isna(row["STN"])) else 0
            except (TypeError, ValueError):
                stn = 0
            t = row["TIME"]
            try:
                tstr = t.strftime("%y%m%d/%H%M") if (t is not None and not pd.isna(t)) else "-9999"
            except Exception:
                tstr = "-9999"
            toks = [str(stn), tstr] + [_fmt(row[p]) for p in params]
            first = toks[:8]
            lines.append(" ".join(first))
            for grp in _chunk(toks[8:], 6):
                lines.append(" ".join(grp))
        # two blank lines to close the file (official BUFKIT format)
        lines.append("")
        lines.append("")

    return "\n".join(lines) + "\n"


def write_buf(bf, path: str, **kw) -> None:
    text = to_buf_text(bf, **kw)
    with open(path, "w", newline="\r\n") as fh:
        fh.write(text)
