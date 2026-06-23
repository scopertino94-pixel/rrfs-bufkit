"""
Shared BUFKIT .buf field schemas.

These three lists define the field layout the writer emits and the decoder fills,
so both sides agree on order:
  SNPARM     - per-level profile columns (pressure, temperature, wind, ...).
  STNPRM     - the derived station/index block (CAPE, LIFT, PWAT, ...).
  SFC_PARAMS - the surface time-series fields.

rrfs_bufr.py imports these. (The module keeps the name grib_to_buf.py for
historical reasons; it now holds only the schema definitions.)
"""

SNPARM = ["PRES", "TMPC", "TMWC", "DWPC", "THTE", "DRCT", "SKNT", "OMEG", "CFRL", "HGHT"]
STNPRM = ["SHOW", "LIFT", "SWET", "KINX", "LCLP", "PWAT", "TOTL", "CAPE",
          "LCLT", "CINS", "EQLV", "LFCT", "BRCH"]
SFC_PARAMS = ["PMSL", "PRES", "SKTC", "STC1", "SNFL", "WTNS", "P01M", "C01M", "STC2",
              "LCLD", "MCLD", "HCLD", "SNRA", "UWND", "VWND", "R01M", "BFGR", "T2MS",
              "Q2MS", "WXTS", "WXTP", "WXTZ", "WXTR", "USTM", "VSTM", "HLCY", "SLLH",
              "WSYM", "CDBP", "VSBK", "TD2M"]
