"""
Scan RRFS bufr.NNNNNN files on S3 to build station_id -> file_key mapping.

Strategy:
  1. List ALL objects in the S3 prefix to get actual filenames
     (file numbers are WMO station numbers, not sequential).
  2. Build a pybufrkit table_root ONCE from a local sample file.
  3. Download only the first 22 KB of each file via S3 Range request -
     enough to reach msg3 (first dataCategory=241 message, ~10.5 KB offset).
  4. Decode msg3 to read RPID (001198), lat, lon.
  5. Save station_id -> file_key mapping as JSON.

Usage:
    python tools/scan_rrfs_rpids.py [options]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

BUCKET = "noaa-rrfs-pds"
RANGE_BYTES = 22000   # first 22 KB; table msgs end at ~10.5 KB, msg3 ends at ~19 KB


def _build_tables_from_sample(sample_path: str):
    """Build (Decoder, DataQuerent) from a local sample file."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from bufkit_replica.rrfs_bufr import _split_messages, load_all_tables, build_table_root
    from pybufrkit.decoder import Decoder
    from pybufrkit.dataquery import NodePathParser, DataQuerent

    data = open(sample_path, "rb").read()
    msgs = _split_messages(data)
    B, D, hdr = load_all_tables(msgs)
    root = build_table_root(B, D, hdr)
    decoder = Decoder(tables_root_dir=root)
    querent = DataQuerent(NodePathParser())
    return decoder, querent


def _msg_starts(data: bytes) -> list[tuple[int, int]]:
    """Return (offset, size) for each BUFR message in raw bytes."""
    result = []
    i = 0
    while i < len(data) - 7:
        if data[i:i + 4] == b"BUFR":
            sz = int.from_bytes(data[i + 4:i + 7], "big")
            result.append((i, sz))
            i += sz
        else:
            i += 1
    return result


def _extract_rpid(decoder, querent, raw: bytes):
    """Decode RPID + lat/lon from the first ~22 KB of a station file."""
    import eccodes as ec

    msgs = _msg_starts(raw)
    data_msg = None
    for off, sz in msgs:
        mb = raw[off:min(off + sz, len(raw))]
        if len(mb) < 8:
            continue
        try:
            bid = ec.codes_new_from_message(mb)
            cat = ec.codes_get(bid, "dataCategory")
            ec.codes_release(bid)
        except Exception:
            continue
        if cat == 241:
            data_msg = mb
            break

    if data_msg is None:
        return None, None, None

    try:
        m = decoder.process(data_msg)
    except Exception:
        return None, None, None

    def _scalar(desc, si=0):
        try:
            vals = querent.query(m, desc).all_values()[si]
            def first(x):
                if isinstance(x, list):
                    return first(x[0]) if x else None
                return x
            v = first(vals)
            return v.value if hasattr(v, "value") else v
        except Exception:
            return None

    rpid = _scalar("001198")
    if isinstance(rpid, bytes):
        rpid = rpid.decode("ascii", errors="ignore")
    rpid = str(rpid or "").strip() or None

    lat = _scalar("005002")
    lon = _scalar("006002")
    return rpid, (float(lat) if lat is not None else None), (float(lon) if lon is not None else None)


def scan_key(s3_client, key: str, decoder, querent):
    """Download first 22 KB of one S3 key and extract RPID."""
    try:
        resp = s3_client.get_object(
            Bucket=BUCKET, Key=key,
            Range=f"bytes=0-{RANGE_BYTES - 1}"
        )
        raw = resp["Body"].read()
    except Exception as e:
        return key, None, None, None

    rpid, lat, lon = _extract_rpid(decoder, querent, raw)
    return key, rpid, lat, lon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="20260607")
    ap.add_argument("--hour", default="00")
    ap.add_argument("--member", default="m001")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--sample", default=None)
    ap.add_argument("--out", default="samples/rrfs/rpid_map.json")
    args = ap.parse_args()

    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    # Locate sample for table extraction
    sample = args.sample
    if sample is None:
        for candidate in [
            f"samples/rrfs/bufr.000001.{args.date}{args.hour}",
            "samples/rrfs/bufr.000001.2026060700",
        ]:
            if os.path.exists(candidate):
                sample = candidate
                break
    if not sample or not os.path.exists(sample):
        print("ERROR: no local sample file found. Use --sample <path>")
        sys.exit(1)

    print(f"Building table root from {sample} ...", flush=True)
    decoder, querent = _build_tables_from_sample(sample)
    print("Tables ready.\n", flush=True)

    prefix = f"rrfs_a/rrfsens.{args.date}/{args.hour}/{args.member}/bufr.00"
    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    print("Listing files ...", flush=True)
    paginator = s3.get_paginator("list_objects_v2")
    all_keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix + "/"):
        for obj in page.get("Contents", []):
            all_keys.append(obj["Key"])
    total = len(all_keys)
    print(f"Found {total} files.\n", flush=True)

    rpid_map: dict[str, str] = {}   # key -> rpid
    lat_map:  dict[str, float] = {}
    lon_map:  dict[str, float] = {}
    lock = threading.Lock()
    done_n = [0]

    def _cb(fut):
        key, rpid, lat, lon = fut.result()
        fname = key.split("/")[-1]
        with lock:
            done_n[0] += 1
            n = done_n[0]
            if n % 200 == 0 or n == total:
                print(f"  {n}/{total} ({100*n/total:.0f}%)  mapped: {len(rpid_map)}", flush=True)
            if rpid:
                rpid_map[fname] = rpid
                if lat is not None:
                    lat_map[fname] = lat
                if lon is not None:
                    lon_map[fname] = lon

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(scan_key, s3, k, decoder, querent)
            for k in all_keys
        ]
        for f in futures:
            f.add_done_callback(_cb)
        for f in futures:
            f.result()

    # Build final map: rpid -> filename (and reverse)
    rpid_to_file = {v: k for k, v in rpid_map.items()}
    out = {
        "cycle": f"{args.date}{args.hour}",
        "member": args.member,
        "rpid_to_file": rpid_to_file,
        "file_to_rpid": rpid_map,
        "file_to_lat": lat_map,
        "file_to_lon": lon_map,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {len(rpid_map)} entries -> {args.out}", flush=True)

    # WW station summary
    ww_path = os.path.join(os.path.dirname(__file__), "..", "bufkit_replica", "stations_ww.txt")
    if os.path.exists(ww_path):
        ww = [s.strip().upper() for s in open(ww_path).read().splitlines() if s.strip()]
        found = [(s, rpid_to_file[s]) for s in ww if s in rpid_to_file]
        missing = [s for s in ww if s not in rpid_to_file]
        print(f"\nWW stations found: {len(found)}/{len(ww)}")
        if missing:
            print(f"Missing ({len(missing)}): {', '.join(missing)}")
        print("\nSample mappings (first 30):")
        for s, fname in found[:30]:
            la = lat_map.get(fname, "?")
            lo = lon_map.get(fname, "?")
            print(f"  {s:8s}  {fname}  lat={la}  lon={lo}")


if __name__ == "__main__":
    main()
