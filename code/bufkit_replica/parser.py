"""
BUFKIT .buf file parser.

The .buf file is plain ASCII with two sections:

  1. Upper-air section: repeating blocks, one per forecast time. Each block has
     key=value station/derived parameters followed by a numeric vertical profile.
     The profile column order is given by the `SNPARM = ...` header line; the
     derived (station) parameter names by the `STNPRM = ...` line.

  2. Surface section: begins at the line containing "STN YYMMDD/HHMM"; a
     multi-line column header followed by one (multi-line) record per time.

This module turns that into clean Python objects with no plotting dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

MISSING = -9999.00


@dataclass
class Sounding:
    """One forecast time: station info, derived indices, and the vertical profile."""
    stid: str
    stnm: int
    valid_time: datetime
    slat: float
    slon: float
    selv: float
    derived: dict[str, float]              # SHOW, LIFT, CAPE, CINS, PWAT, ...
    levels: pd.DataFrame                    # columns = SNPARM (PRES, TMPC, DWPC, ...)


@dataclass
class BufkitFile:
    """A parsed .buf file: the SNPARM/STNPRM schemas, all soundings, surface series."""
    snparm: list[str]
    stnprm: list[str]
    soundings: list[Sounding] = field(default_factory=list)
    surface: pd.DataFrame | None = None

    @property
    def station(self) -> str:
        return self.soundings[0].stid if self.soundings else ""

    @property
    def times(self) -> list[datetime]:
        return [s.valid_time for s in self.soundings]


def _to_float(tok: str) -> float:
    try:
        v = float(tok)
    except ValueError:
        return np.nan
    return np.nan if v == MISSING else v


def _parse_kv(text: str) -> dict[str, float]:
    """Parse 'A = 1.0 B = 2.0' style strings into {A: 1.0, B: 2.0}."""
    toks = text.replace(" = ", "=").split()
    out: dict[str, float] = {}
    for tok in toks:
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = _to_float(v)
    return out


def parse(path_or_text: str, *, is_text: bool = False) -> BufkitFile:
    if is_text:
        raw = path_or_text
    else:
        with open(path_or_text, "r", errors="replace") as fh:
            raw = fh.read()

    lines = raw.splitlines()

    # --- locate schema + the split between upper-air and surface sections ---
    snparm: list[str] = []
    stnprm: list[str] = []
    sfc_start = len(lines)
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("SNPARM"):
            snparm = s.split("=", 1)[1].replace(" ", "").split(";")
        elif s.startswith("STNPRM"):
            stnprm = s.split("=", 1)[1].replace(" ", "").split(";")
        elif s.startswith("STN YYMMDD/HHMM"):
            sfc_start = i
            break

    upper = lines[:sfc_start]
    surface_lines = lines[sfc_start:]

    soundings = _parse_upper(upper, snparm, stnprm)
    surface = _parse_surface(surface_lines)

    return BufkitFile(snparm=snparm, stnprm=stnprm, soundings=soundings, surface=surface)


def _parse_upper(lines: list[str], snparm: list[str], stnprm: list[str]) -> list[Sounding]:
    # Split into per-time blocks at each "STID = ..." line.
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for line in lines:
        if line.strip().startswith("STID"):
            if cur is not None:
                blocks.append(cur)
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        blocks.append(cur)

    ncol = len(snparm)
    soundings: list[Sounding] = []
    for block in blocks:
        # Everything before the profile is key=value metadata; the profile is the
        # run of pure-numeric tokens after the SNPARM column-name header lines.
        meta: dict[str, float | str] = {}
        numeric_tokens: list[str] = []
        seen_profile_header = False
        for line in block:
            s = line.strip()
            if not s:
                continue
            # The profile column header (e.g. "PRES TMPC TMWC ...") - skip, it just
            # restates SNPARM, possibly wrapped over two lines.
            if any(s.startswith(p) for p in (snparm[:1] or ["PRES"])) and "=" not in s:
                seen_profile_header = True
                continue
            if seen_profile_header and all(c not in s for c in "="):
                # numeric profile rows (also skip a possible 2nd header line)
                toks = s.split()
                if all(_is_number(t) for t in toks):
                    numeric_tokens.extend(toks)
                continue
            if "=" in s:
                kv = _parse_kv(s)
                meta.update(kv)
                # STID and TIME are text; re-capture raw AFTER the numeric merge so
                # the float pass (which turns 'KBUF'/'260607/1100' into NaN) can't win.
                if "STID" in s or "TIME" in s:
                    parts = s.replace(" = ", "=").split()
                    for p in parts:
                        if p.startswith("STID="):
                            meta["STID"] = p.split("=", 1)[1]
                        elif p.startswith("TIME="):
                            meta["TIME_RAW"] = p.split("=", 1)[1]

        # Build the level DataFrame
        rows = [numeric_tokens[i:i + ncol] for i in range(0, len(numeric_tokens), ncol)]
        rows = [r for r in rows if len(r) == ncol]
        levels = pd.DataFrame(rows, columns=snparm, dtype=float)
        levels = levels.replace(MISSING, np.nan)

        valid = _decode_time(meta.get("TIME_RAW"))
        derived = {k: float(v) for k, v in meta.items()
                   if k in stnprm and isinstance(v, (int, float))}

        soundings.append(Sounding(
            stid=str(meta.get("STID", "")),
            stnm=int(meta.get("STNM", 0)) if not np.isnan(meta.get("STNM", np.nan)) else 0,
            valid_time=valid,
            slat=float(meta.get("SLAT", np.nan)),
            slon=float(meta.get("SLON", np.nan)),
            selv=float(meta.get("SELV", np.nan)),
            derived=derived,
            levels=levels,
        ))
    return soundings


def _parse_surface(lines: list[str]) -> pd.DataFrame | None:
    if not lines:
        return None
    # Collect the multi-line header (until the first token is a station number),
    # then group remaining tokens by header length.
    header_tokens: list[str] = []
    data_tokens: list[str] = []
    in_data = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        toks = s.split()
        if not in_data:
            # header lines are the ones with alpha column names
            if toks[0] == "STN" or not _is_number(toks[0]):
                header_tokens.extend(toks)
                continue
            in_data = True
        data_tokens.extend(toks)

    # Rename the date/time column for clarity
    headers = ["TIME" if h == "YYMMDD/HHMM" else h for h in header_tokens]
    ncol = len(headers)
    if ncol == 0:
        return None
    rows = [data_tokens[i:i + ncol] for i in range(0, len(data_tokens), ncol)]
    rows = [r for r in rows if len(r) == ncol]
    df = pd.DataFrame(rows, columns=headers)
    df["TIME"] = pd.to_datetime(df["TIME"], format="%y%m%d/%H%M", errors="coerce")
    for c in df.columns:
        if c not in ("TIME",):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.replace(MISSING, np.nan)
    return df


def _decode_time(val) -> datetime | None:
    if val is None:
        return None
    s = str(val)
    # TIME arrives parsed as float by _parse_kv (e.g. 260607/1100 -> NaN), so re-read
    # raw: handle the 'YYMMDD/HHMM' string form.
    if "/" in s:
        try:
            return datetime.strptime(s, "%y%m%d/%H%M")
        except ValueError:
            return None
    return None


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False
