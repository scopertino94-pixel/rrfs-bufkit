"""
Decode NCEP class-1 forecast-sounding BUFR (RRFS `bufr.NNNNNN` files) to BUFKIT.

These files carry their own NCEP-local BUFR tables in the first message (a
dataCategory-11 "table" message). Stock eccodes/pybufrkit can't expand the local
descriptors (sub-centre 3) without those tables, so this module:

  1. reads the embedded Table B (elements) + Table D (sequences) out of message 0
     via eccodes (which decodes the *table* message fine),
  2. merges them with pybufrkit's bundled WMO tables into a custom table root,
  3. decodes the data messages (per-forecast-hour profiles + surface) with
     pybufrkit pointed at that root,
  4. maps the NCEP mnemonics to BUFKIT SNPARM/surface fields.

Pure-Python + eccodes; no Fortran/NCEPLIBS build required (works on Windows).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading

import numpy as np
import eccodes as ec

# MetPy's parcel ascent (moist_lapse) uses scipy odeint which is NOT thread-safe.
# Only the per-sounding convective-index parcel calls need it now; the per-level
# thermo below is pure-numpy closed form and runs lock-free.
_METPY_LOCK = threading.Lock()

# Convective indices BUFKIT expects in the STNPRM block. RRFS BUFR does not carry
# these (raw profile only), so we compute them like NWS's BUFR->BUFKIT converter.
_STNPRM = ["SHOW", "LIFT", "SWET", "KINX", "LCLP", "PWAT", "TOTL", "CAPE",
           "LCLT", "CINS", "EQLV", "LFCT", "BRCH"]


def _sat_vapor_pa(Tc):
    """Saturation vapor pressure (Pa) over water, Bolton (1980)."""
    return 611.2 * np.exp(17.67 * Tc / (Tc + 243.5))


def _dewpoint_c(P_pa, SH):
    """Dewpoint (degC) from specific humidity (kg/kg) and pressure (Pa)."""
    SH = np.clip(SH, 1e-9, None)
    e = SH * P_pa / (0.622 + 0.378 * SH)          # vapor pressure, Pa
    e = np.clip(e, 1e-3, None)
    ln = np.log(e / 611.2)
    return 243.5 * ln / (17.67 - ln)


def _relhum(P_pa, Tk, SH):
    """Relative humidity (%) from SH and pressure, clipped to (1, 100]."""
    SH = np.clip(SH, 1e-9, None)
    e = SH * P_pa / (0.622 + 0.378 * SH)
    es = _sat_vapor_pa(Tk - 273.15)
    return np.clip(100.0 * e / es, 1.0, 100.0)


def _wetbulb_psychro(P_pa, Tc, Tdc):
    """Isobaric (psychrometric) wet-bulb temperature (degC), vectorized.

    Solves  e(Td) = es(Tw) - gamma*P*(T - Tw)  by a few Newton iterations.
    Valid across the whole atmosphere (unlike Stull 2011, which yields Tw > T in
    cold/dry upper levels - that bug crashed BUFKIT with run-time error 5).
    Result is physically clamped to [Td, T].
    """
    P_hpa = P_pa / 100.0
    e = _sat_vapor_pa(Tdc) / 100.0                # actual vapor pressure, hPa
    Tw = 0.5 * (Tc + Tdc)                          # initial guess
    for _ in range(8):
        es = _sat_vapor_pa(Tw) / 100.0
        des = es * 17.67 * 243.5 / (Tw + 243.5) ** 2          # d(es)/dTw
        gamma = 6.60e-4 * (1.0 + 0.00115 * Tw)               # psychrometer "constant"
        f = es - gamma * P_hpa * (Tc - Tw) - e
        fp = des + gamma * P_hpa                              # ~d f/dTw
        Tw = Tw - f / fp
    return np.clip(Tw, Tdc, Tc)


def _cloud_frac_rh(rh, rh_crit=80.0):
    """Diagnostic cloud fraction (%) from relative humidity.

    RRFS BUFR carries the cloud-cover descriptor (020198) but leaves it empty, so
    every level would be missing. BUFKIT requires a populated CFRL column - an
    all-missing one crashes it on load (run-time error 5). Real model .buf files
    always populate CFRL, so we diagnose it from RH (clouds where RH is high), the
    standard fallback when a model doesn't export cloud fraction.
    """
    return np.clip((rh - rh_crit) / (100.0 - rh_crit), 0.0, 1.0) * 100.0


def _thetae_bolton(P_pa, Tk, Tdc, SH):
    """Equivalent potential temperature (K), Bolton (1980) eq. 38/43."""
    SH = np.clip(SH, 1e-9, None)
    r = SH / (1.0 - SH)                            # mixing ratio kg/kg
    r_gkg = r * 1000.0
    Tdk = Tdc + 273.15
    # temperature at the LCL (Bolton eq. 15)
    TL = 56.0 + 1.0 / (1.0 / (Tdk - 56.0) + np.log(Tk / Tdk) / 800.0)
    p_hpa = P_pa / 100.0
    theta_dl = Tk * (1000.0 / p_hpa) ** (0.2854 * (1.0 - 0.28e-3 * r_gkg))
    return theta_dl * np.exp((3.376 / TL - 0.00254) * r_gkg * (1.0 + 0.81e-3 * r_gkg))


def _sweat_index(P_hPa, Tdc, U_ms, V_ms, totl):
    """SWEAT index (Miller 1972). Winds in knots; directional term gated by the
    standard veering/speed conditions. Returns nan if 850/500 unavailable."""
    if totl != totl:
        return np.nan
    Pa = P_hPa[::-1]                       # ascending for np.interp
    def at(pl, X): return float(np.interp(pl, Pa, X[::-1]))
    if P_hPa.min() > 500 or P_hPa.max() < 850:
        return np.nan
    td850 = at(850, Tdc)
    u850, v850 = at(850, U_ms), at(850, V_ms)
    u500, v500 = at(500, U_ms), at(500, V_ms)
    KT = 1.943844
    spd850 = np.hypot(u850, v850) * KT
    spd500 = np.hypot(u500, v500) * KT
    dir850 = (270.0 - np.degrees(np.arctan2(v850, u850))) % 360.0
    dir500 = (270.0 - np.degrees(np.arctan2(v500, u500))) % 360.0
    term_td = 12.0 * max(td850, 0.0)
    term_tt = 20.0 * max(totl - 49.0, 0.0)
    shear = 0.0
    if (130 <= dir850 <= 250 and 210 <= dir500 <= 310
            and (dir500 - dir850) > 0 and spd850 >= 15 and spd500 >= 15):
        shear = 125.0 * (np.sin(np.radians(dir500 - dir850)) + 0.2)
    return float(term_td + term_tt + 2.0 * spd850 + spd500 + shear)


def _bulk_richardson(cape, U_ms, V_ms, hght_m):
    """Bulk Richardson Number = CAPE / (0.5 * shear^2), shear = |meanwind(0-6km)
    - meanwind(0-500m)| (m/s). Returns nan for tiny shear (avoids blow-up)."""
    if cape is None or cape != cape or cape <= 0:
        return np.nan
    agl = hght_m - hght_m[0]
    def mean(lo, hi):
        m = (agl >= lo) & (agl <= hi)
        return (U_ms[m].mean(), V_ms[m].mean()) if m.any() else (np.nan, np.nan)
    u6, v6 = mean(0, 6000); u0, v0 = mean(0, 500)
    shear = np.hypot(u6 - u0, v6 - v0)
    if not np.isfinite(shear) or shear < 1.0:
        return np.nan
    return float(cape / (0.5 * shear * shear))


def _convective_indices(P_hPa, Tc, Tdc):
    """Compute BUFKIT STNPRM indices from a profile (surface-first, P descending).

    One surface-based parcel ascent, reused for CAPE/CIN/LI/LFC/EL. Wrapped so
    any failure (bad profile, MetPy edge case) yields NaN for that field rather
    than aborting the whole file. SWET/BRCH are left NaN (need layer winds).
    """
    out = {k: np.nan for k in _STNPRM}
    try:
        from metpy.calc import (parcel_profile, cape_cin, lifted_index, lcl, lfc, el,
                                 precipitable_water, k_index, total_totals_index,
                                 showalter_index)
        from metpy.units import units
        # need monotonically decreasing pressure and finite T/Td
        good = np.isfinite(P_hPa) & np.isfinite(Tc) & np.isfinite(Tdc)
        P_hPa, Tc, Tdc = P_hPa[good], Tc[good], Tdc[good]
        if len(P_hPa) < 5 or P_hPa[0] <= P_hPa[-1]:
            return out
        Tdc = np.minimum(Tdc, Tc)                  # dewpoint can't exceed temp
        p = P_hPa * units.hPa
        T = Tc * units.degC
        Td = Tdc * units.degC
        with _METPY_LOCK:
            prof = parcel_profile(p, T[0], Td[0]).to("degC")
            cape, cin = cape_cin(p, T, Td, prof)
            li = lifted_index(p, T, prof)
            lclp, lclt = lcl(p[0], T[0], Td[0])
            try:    lfcp, _ = lfc(p, T, Td, parcel_temperature_profile=prof)
            except Exception: lfcp = None
            try:    elp, _ = el(p, T, Td, parcel_temperature_profile=prof)
            except Exception: elp = None
            pwat = precipitable_water(p, Td)
            kidx = k_index(p, T, Td)
            tt = total_totals_index(p, T, Td)
            try:    shx = showalter_index(p, T, Td)
            except Exception: shx = None
        out["CAPE"] = max(float(cape.m_as("joule/kilogram")), 0.0)
        out["CINS"] = float(cin.m_as("joule/kilogram"))
        out["LIFT"] = float(np.ravel(li.m)[0])
        out["LCLP"] = float(lclp.m_as("hPa"))
        out["LCLT"] = float(lclt.m_as("kelvin"))     # BUFKIT expects LCL temp in KELVIN, not degC
        out["PWAT"] = float(pwat.m_as("millimeter"))
        out["KINX"] = float(kidx.m_as("degC"))      # K-index is absolute degC (~20-40)
        out["TOTL"] = float(tt.m_as("delta_degC"))
        if lfcp is not None and np.isfinite(lfcp.m): out["LFCT"] = float(lfcp.m_as("hPa"))
        if elp  is not None and np.isfinite(elp.m):  out["EQLV"] = float(elp.m_as("hPa"))
        if shx  is not None: out["SHOW"] = float(np.ravel(shx.m)[0])
        # LFC and EL must be all-or-nothing: BUFKIT crashes (run-time error 5) on an
        # EL with no LFC. MetPy's el() can return a spurious EL for marginal/capped
        # parcels where lfc() gives up - drop both if either is missing.
        if not (np.isfinite(out["LFCT"]) and np.isfinite(out["EQLV"])):
            out["LFCT"] = out["EQLV"] = np.nan
    except Exception:
        pass
    return out


def _split_messages(data: bytes) -> list[bytes]:
    """Split a multi-message BUFR file into individual message byte strings."""
    out = []
    i = 0
    while True:
        s = data.find(b"BUFR", i)
        if s < 0:
            break
        # Section 0 total length is bytes 4..7 (24-bit) for edition >= 2
        total = int.from_bytes(data[s + 4:s + 7], "big")
        if total <= 0 or s + total > len(data):
            # fall back: next BUFR or EOF
            nxt = data.find(b"BUFR", s + 4)
            total = (nxt - s) if nxt > 0 else (len(data) - s)
        out.append(data[s:s + total])
        i = s + total
    return out


def extract_embedded_tables(table_msg_bytes: bytes):
    """Parse message 0 into (tableB, tableD, header) reconstructed from the file."""
    bid = ec.codes_new_from_message(table_msg_bytes)
    ec.codes_set(bid, "unpack", 1)

    header = {
        "master_table": ec.codes_get(bid, "masterTableNumber"),
        "master": ec.codes_get(bid, "masterTablesVersionNumber"),
        "centre": ec.codes_get(bid, "bufrHeaderCentre"),
        "subcentre": ec.codes_get(bid, "bufrHeaderSubCentre"),
        "local": ec.codes_get(bid, "localTablesVersionNumber"),
    }

    # Ordered walk of keys so Table-D members attach to the right sequence.
    it = ec.codes_bufr_keys_iterator_new(bid)
    ordered = []
    while ec.codes_bufr_keys_iterator_next(it):
        nm = ec.codes_bufr_keys_iterator_get_name(it)
        ordered.append(nm)
    ec.codes_bufr_keys_iterator_delete(it)

    def g(name):
        try:
            return ec.codes_get(bid, name)
        except Exception:
            return None

    tableB, tableD = {}, {}
    cur = None  # current descriptor being defined
    for nm in ordered:
        base = re.sub(r"^#\d+#", "", nm)
        if base == "fDescriptorToBeAddedOrDefined":
            idx = re.match(r"^#(\d+)#", nm).group(1)
            f = int(g(nm))
            x = int(g(f"#{idx}#xDescriptorToBeAddedOrDefined"))
            y = int(g(f"#{idx}#yDescriptorToBeAddedOrDefined"))
            desc = f"{f}{x:02d}{y:03d}"
            cur = {"f": f, "desc": desc, "members": [], "attrs": {}}
            if f == 0:
                tableB[desc] = cur
            elif f == 3:
                tableD[desc] = cur
        elif cur is not None and cur["f"] == 0:
            if base in ("elementNameLine1", "elementNameLine2", "unitsName",
                        "unitsScaleSign", "unitsScale", "unitsReferenceSign",
                        "unitsReferenceValue", "elementDataWidth"):
                cur["attrs"].setdefault(base, g(nm))
        elif cur is not None and cur["f"] == 3:
            if base == "descriptorDefiningSequence":
                v = int(g(nm))
                cur["members"].append(f"{v:06d}")
            elif base == "text":
                cur["attrs"].setdefault("text", g(nm))

    # Convert to pybufrkit JSON rows
    B = {}
    for desc, e in tableB.items():
        a = e["attrs"]
        name = (str(a.get("elementNameLine1", "")) + str(a.get("elementNameLine2", ""))).strip()
        unit = str(a.get("unitsName", "")).strip()

        def _neg(sign):
            return str(sign).strip() in ("-", "1")

        scale = int(a.get("unitsScale", 0) or 0)
        if _neg(a.get("unitsScaleSign", "+")):
            scale = -scale
        ref = int(a.get("unitsReferenceValue", 0) or 0)
        if _neg(a.get("unitsReferenceSign", "+")):
            ref = -ref
        width = int(a.get("elementDataWidth", 0) or 0)
        B[desc] = [name, unit, scale, ref, width, unit, scale, width]

    D = {}
    for desc, e in tableD.items():
        D[desc] = [str(e["attrs"].get("text", "")).strip(), e["members"]]

    ec.codes_release(bid)
    return B, D, header


def load_all_tables(msgs):
    """Merge embedded tables from every leading table (dataCategory 11) message.

    NCEP splits the table definitions across the first few messages, so a single
    message gives an incomplete Table D (missing referenced sub-sequences).
    """
    B, D, header = {}, {}, None
    for mb in msgs:
        bid = ec.codes_new_from_message(mb)
        cat = ec.codes_get(bid, "dataCategory")
        ec.codes_release(bid)
        if cat != 11:
            break
        b, d, h = extract_embedded_tables(mb)
        B.update(b)
        D.update(d)
        if header is None:
            header = h
    _inline_replication_helpers(D)
    return B, D, header


def _inline_replication_helpers(D):
    """Inline delayed-replication helper sequences (e.g. 360002 = [1-01-000, 0-31-001])
    into their parents. NCEP wraps the level replication in a tiny sub-sequence;
    pybufrkit won't let the replication operator cross that sequence boundary, so
    the level block decodes only once. Inlining puts the operator directly before
    the block it must replicate.
    """
    helpers = {k for k, v in D.items()
               if len(v[1]) == 2 and v[1][0].startswith("1") and v[1][1].startswith("031")}
    for k, (title, mem) in list(D.items()):
        out = []
        for d in mem:
            out.extend(D[d][1] if d in helpers else [d])
        D[k] = [title, out]


def build_table_root(B, D, header) -> str:
    """Write a pybufrkit table root merging WMO base tables with the embedded local ones."""
    import pybufrkit
    pk_root = os.path.join(os.path.dirname(pybufrkit.__file__), "tables")
    master = header["master"]
    # WMO base for this master version (fall back to latest available)
    base_dir = os.path.join(pk_root, "0", "0_0", str(master))
    if not os.path.isdir(base_dir):
        versions = sorted(int(v) for v in os.listdir(os.path.join(pk_root, "0", "0_0")))
        base_dir = os.path.join(pk_root, "0", "0_0", str(versions[-1]))

    tmp = tempfile.mkdtemp(prefix="rrfs_bufrtab_")
    mt = str(header.get("master_table", 0))
    # pybufrkit locates tables as <masterTableNumber>/<centre>_<subcentre>/<masterVersion>/
    cs = f"{header['centre']}_{header['subcentre']}"
    dest = os.path.join(tmp, mt, cs, str(master))
    os.makedirs(dest, exist_ok=True)

    baseB = json.load(open(os.path.join(base_dir, "TableB.json")))
    baseD = json.load(open(os.path.join(base_dir, "TableD.json")))
    baseB.update(B)
    baseD.update(D)

    # The RRFS *data* messages declare localTablesVersionNumber=0, so pybufrkit
    # uses ONLY the WMO base (0_0/<master>) and never loads a local table. So we
    # merge the embedded NCEP elements straight into that base dir, and also write
    # a proper local dir for completeness.
    def _write(d):
        os.makedirs(d, exist_ok=True)
        json.dump(baseB, open(os.path.join(d, "TableB.json"), "w"))
        json.dump(baseD, open(os.path.join(d, "TableD.json"), "w"))
        for fn in ("TableA.json", "code_and_flag.json"):
            src = os.path.join(base_dir, fn)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(d, fn))

    _write(os.path.join(tmp, mt, "0_0", str(master)))      # always-loaded WMO base
    _write(dest)                                            # local dir (centre_subcentre/local)
    return tmp


def _flat(querent, m, desc, subset):
    """Flattened list of a descriptor's values for one subset."""
    try:
        vals = querent.query(m, desc).all_values()[subset]
    except Exception:
        return []
    out = []
    def rec(x):
        if isinstance(x, list):
            for y in x:
                rec(y)
        else:
            out.append(x)
    rec(vals)
    return out


def _scalar(querent, m, desc, subset):
    v = _flat(querent, m, desc, subset)
    return v[0] if v else None


def make_decoder(sample_path=None, sample_bytes=None):
    """Build a (Decoder, DataQuerent) from a sample BUFR file.

    Pre-building the decoder is critical for batch processing: eccodes (used for
    table extraction) is NOT thread-safe and will crash if called concurrently.
    Call this ONCE in the main thread, then pass dec/q to decode_to_bufkit_fast
    from any number of worker threads.

    Args:
        sample_path: path to a local .bufr file to extract tables from
        sample_bytes: raw bytes of a .bufr file (alternative to sample_path)
    """
    from pybufrkit.decoder import Decoder
    from pybufrkit.dataquery import NodePathParser, DataQuerent

    if sample_bytes is not None:
        data = sample_bytes
    elif sample_path is not None:
        data = open(sample_path, "rb").read()
    else:
        raise ValueError("Provide sample_path or sample_bytes")

    msgs = _split_messages(data)
    B, D, hdr = load_all_tables(msgs)
    root = build_table_root(B, D, hdr)
    dec = Decoder(tables_root_dir=root)
    q = DataQuerent(NodePathParser())
    return dec, q


def decode_to_bufkit(path, cycle=None, dec=None, q=None):
    """Decode an RRFS NCEP sounding BUFR file into a BufkitFile (all forecast hours).

    Args:
        path: path to bufr.NNNNNN file
        cycle: model cycle as string (e.g. '2026-06-07 00:00') or None
        dec: pre-built pybufrkit Decoder (build with make_decoder() once per process)
        q:   pre-built DataQuerent (from make_decoder())

    If dec/q are not provided, they are built from `path` itself (slower; not
    thread-safe due to eccodes).  For batch use, always pass pre-built dec/q.
    """
    import numpy as np
    import pandas as pd
    from pybufrkit.decoder import Decoder
    from pybufrkit.dataquery import NodePathParser, DataQuerent

    from .parser import BufkitFile, Sounding
    from .grib_to_buf import SNPARM, STNPRM, SFC_PARAMS

    import re as _re
    # Extract WMO station number from filename: bufr.NNNNNN.YYYYMMDDHH
    _m = _re.search(r"bufr\.(\d+)\.", os.path.basename(path))
    file_stnm = int(_m.group(1)) if _m else 0

    data = open(path, "rb").read()
    msgs = _split_messages(data)

    if dec is None or q is None:
        # Single-file path: build tables from this file (not thread-safe)
        B, D, hdr = load_all_tables(msgs)
        root = build_table_root(B, D, hdr)
        dec = Decoder(tables_root_dir=root)
        q = DataQuerent(NodePathParser())

    cyc = pd.to_datetime(cycle) if cycle else None
    soundings, sfc_rows = [], []
    stid = ""
    for mb in msgs:
        bid = ec.codes_new_from_message(mb)
        cat = ec.codes_get(bid, "dataCategory")
        ec.codes_release(bid)
        if cat != 241:
            continue
        m = dec.process(mb)
        ns = m.n_subsets.value
        for si in range(ns):
            rpid = _scalar(q, m, "001198", si)
            stid = (rpid.decode().strip() if isinstance(rpid, bytes) else str(rpid or "")).strip()
            lat = _scalar(q, m, "005002", si)
            lon = _scalar(q, m, "006002", si)
            elev = _scalar(q, m, "010194", si) or 0.0
            ftim = _scalar(q, m, "004194", si) or 0   # seconds

            P = np.array(_flat(q, m, "010004", si), float)        # Pa
            Tk = np.array(_flat(q, m, "012001", si), float)       # K
            U = np.array(_flat(q, m, "011003", si), float)
            V = np.array(_flat(q, m, "011004", si), float)
            SH = np.array(_flat(q, m, "013001", si), float)       # kg/kg
            OM = np.array(_flat(q, m, "011229", si), float)       # Pa/s
            CF = np.array(_flat(q, m, "020198", si), float)       # %
            n = min(len(P), len(Tk), len(U), len(V), len(SH))
            if n == 0:
                continue
            P, Tk, U, V, SH = P[:n], Tk[:n], U[:n], V[:n], SH[:n]
            OM = OM[:n] if len(OM) >= n else np.full(n, np.nan)
            CF = CF[:n] if len(CF) >= n else np.full(n, np.nan)

            # Per-level thermo: pure-numpy closed form (lock-free, vectorized).
            Tc   = Tk - 273.15
            Tdc  = np.minimum(_dewpoint_c(P, SH), Tc)   # Td can't exceed T
            rh   = np.clip(100.0 * _sat_vapor_pa(Tdc) / _sat_vapor_pa(Tc), 0.0, 100.0)
            tmwc = _wetbulb_psychro(P, Tc, Tdc)
            thte = _thetae_bolton(P, Tk, Tdc, SH)
            drct = (270.0 - np.degrees(np.arctan2(V, U))) % 360.0   # dir wind is FROM
            sknt = np.sqrt(U * U + V * V) * 1.943844                # m/s -> knots
            hght = _hydrostatic_height(P, Tk, SH, float(elev))

            levels = pd.DataFrame({
                "PRES": P / 100.0, "TMPC": Tc, "TMWC": tmwc,
                "DWPC": Tdc, "THTE": thte, "DRCT": drct, "SKNT": sknt,
                "OMEG": OM,
                "CFRL": np.where(np.isfinite(CF), CF, _cloud_frac_rh(rh)),  # RRFS omits cloud -> diagnose from RH
                "HGHT": hght,
            })[SNPARM]

            # Convective indices from the full profile (computed before capping).
            indices = _convective_indices(P / 100.0, Tc, Tdc)
            indices["SWET"] = _sweat_index(P / 100.0, Tdc, U, V, indices["TOTL"])
            indices["BRCH"] = _bulk_richardson(indices["CAPE"], U, V, hght)
            # BUFKIT has been observed with max 62 levels (GFS3); cap at 64 to be safe.
            # Drop the topmost (lowest-pressure) levels - they are deep stratosphere.
            MAX_LEVELS = 64
            if len(levels) > MAX_LEVELS:
                levels = levels.iloc[:MAX_LEVELS].reset_index(drop=True)

            valid = (cyc + pd.Timedelta(seconds=int(ftim))) if cyc is not None else None
            soundings.append(Sounding(
                stid=stid, stnm=file_stnm,
                valid_time=(valid.to_pydatetime() if valid is not None else None),
                slat=float(lat) if lat is not None else 0.0,
                slon=float(lon) if lon is not None else 0.0,
                selv=float(elev),
                derived=indices, levels=levels))

            sfc = {p_: np.nan for p_ in SFC_PARAMS}
            sfc["STN"] = file_stnm
            sfc["TIME"] = valid
            # Real BUFKIT files always populate ~23 surface fields; RRFS provides
            # storm motion, helicity, 2 m humidity and precip-type, so write those
            # (BUFKIT's Momentum-Xfer / Bourgouin / SRH displays expect them).
            psfc = _scalar(q, m, "010195", si)
            skt  = _scalar(q, m, "012061", si)
            tp01 = _scalar(q, m, "013019", si)
            u10  = _scalar(q, m, "011196", si)
            v10  = _scalar(q, m, "011197", si)
            q2   = _scalar(q, m, "013198", si)   # 2 m specific humidity (kg/kg)
            ustm = _scalar(q, m, "011231", si)   # storm motion u (m/s)
            vstm = _scalar(q, m, "011232", si)   # storm motion v (m/s)
            hlcy = _scalar(q, m, "011233", si)   # storm-rel helicity (m2/s2)
            nn = lambda v: float(v) if v is not None else np.nan
            sfc["PRES"] = psfc / 100.0 if psfc else np.nan
            sfc["SKTC"] = skt - 273.15 if skt else np.nan
            sfc["P01M"] = nn(tp01)
            sfc["UWND"] = nn(u10)
            sfc["VWND"] = nn(v10)
            sfc["T2MS"] = float(Tc[0])
            sfc["TD2M"] = float(Tdc[0])
            sfc["Q2MS"] = q2 * 1000.0 if q2 is not None else float(SH[0] * 1000.0)  # g/kg
            sfc["USTM"] = nn(ustm)
            sfc["VSTM"] = nn(vstm)
            sfc["HLCY"] = nn(hlcy)
            for key, desc in (("WXTS", "013232"), ("WXTP", "013233"),
                              ("WXTZ", "013234"), ("WXTR", "013235")):
                val = _scalar(q, m, desc, si)
                sfc[key] = float(val) if val is not None else 0.0
            # PMSL: RRFS omits it -> reduce surface pressure to sea level (hypsometric)
            if psfc:
                Tv0 = Tk[0] * (1.0 + 0.608 * SH[0])
                sfc["PMSL"] = (psfc / 100.0) * np.exp(
                    9.80665 * float(elev) / (287.04 * (Tv0 + 0.0065 * float(elev) / 2.0)))
            # Low/mid/high cloud cover from the RH-diagnosed profile (RRFS omits these)
            cfr = levels["CFRL"].values
            pr  = levels["PRES"].values
            def _lyr(lo, hi):
                msk = (pr <= hi) & (pr > lo)
                return float(np.nanmax(cfr[msk])) if msk.any() else 0.0
            sfc["LCLD"] = _lyr(642.0, 1100.0)
            sfc["MCLD"] = _lyr(350.0, 642.0)
            sfc["HCLD"] = _lyr(0.0, 350.0)
            sfc_rows.append(sfc)

    surface = pd.DataFrame(sfc_rows, columns=["STN", "TIME"] + SFC_PARAMS)
    bf = BufkitFile(snparm=SNPARM, stnprm=STNPRM, soundings=soundings, surface=surface)
    return bf, stid


def _hydrostatic_height(P_pa, Tk, SH, z0):
    """Geopotential height (m) by hypsometric integration from the lowest level."""
    import numpy as np
    Rd, g = 287.04, 9.80665
    Tv = Tk * (1 + 0.608 * SH)            # virtual temperature
    z = np.empty(len(P_pa))
    z[0] = z0
    for i in range(1, len(P_pa)):
        Tvm = 0.5 * (Tv[i] + Tv[i - 1])
        z[i] = z[i - 1] + (Rd * Tvm / g) * np.log(P_pa[i - 1] / P_pa[i])
    return z


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "samples/rrfs/bufr.000001.2026060700"
    data = open(path, "rb").read()
    msgs = _split_messages(data)
    print(f"messages: {len(msgs)}")
    B, D, hdr = extract_embedded_tables(msgs[0])
    print("header:", hdr)
    print(f"reconstructed Table B: {len(B)} elements, Table D: {len(D)} sequences")
    # show the sounding-relevant elements
    for desc in ("010004", "012001", "013001", "011003", "011004", "011229", "020198"):
        if desc in B:
            print(f"  B {desc}: {B[desc][:5]}")
    # show the main sounding sequence members
    for desc, (title, members) in D.items():
        print(f"  D {desc} ({len(members)} members): {members}")
