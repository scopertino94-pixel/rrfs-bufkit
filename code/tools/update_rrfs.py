"""
update_rrfs.py  -  Auto-updater for RRFS BUFKIT profiles

Finds the latest available RRFS cycle on S3, downloads all WW station BUFR
soundings, decodes them, and writes BUFKIT-compatible .buf files to the BUFKIT
Data directory.

By DEFAULT this uses the DETERMINISTIC RRFS-A run (the true RRFS forecast) —
S3 path rrfs_a/rrfs.{date}/{hh}/... — NOT an ensemble member. The ensemble
(REFS) path is opt-in only via --source ens (we do not use it).

Usage:
    python update_rrfs.py [options]

Options:
    --outdir DIR       BUFKIT Data directory (default: C:\\Program Files (x86)\\BUFKIT\\Data)
    --source det|ens   det = deterministic RRFS-A (DEFAULT, what we ship);
                       ens = REFS ensemble member (rrfs_a/rrfsens.../{member}/...)
    --member STR       ensemble member, used ONLY with --source ens (default: m001)
    --publish-repo R   upload the .buf to a Hugging Face dataset + squash history
    --workers N        Parallel workers (default: 6)
    --max-age-h N      Skip cycles older than N hours (default: 36)
    --statefile PATH   record last cycle processed; no-op until a new cycle lands
    --logfile PATH     Append log to this file (default: update_rrfs.log next to script)
    --force            Reprocess this cycle even if the state file says it is done
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BUCKET      = "noaa-rrfs-pds"
HOURS       = ["18", "12", "06", "00"]   # synoptic cycles (newest first)
MEMBER      = "m001"
SAMPLE_FILE = "bufr.000001"


def bufr_prefix(source: str, date_str: str, hh: str, member: str) -> str:
    """S3 prefix holding the per-station bufr.NNNNNN files for a cycle.

    NCEP stores them under a cycle-tagged subdir (bufr.{cyc}), not bufr.00.
        det : rrfs_a/rrfs.{date}/{hh}/bufr.{hh}              (deterministic RRFS-A)
        ens : rrfs_a/rrfsens.{date}/{hh}/{member}/bufr.{hh}  (REFS member)
    """
    sub = f"bufr.{hh}"
    if source == "det":
        return f"rrfs_a/rrfs.{date_str}/{hh}/{sub}"
    return f"rrfs_a/rrfsens.{date_str}/{hh}/{member}/{sub}"


# ----------------------------------------------------------------------
# Process-pool worker (MetPy's LSODA parcel ascent is not thread-safe, so the
# per-station decode runs in separate PROCESSES rather than threads). Each
# process builds its own decoder + S3 client once via the initializer.
# ----------------------------------------------------------------------
_WK = {}


def _pp_init(sample_path, cfg):
    import logging as _lg
    _lg.getLogger("pybufrkit").setLevel(_lg.WARNING)
    sys.path.insert(0, ROOT)                       # child (spawn) skips main()
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    from bufkit_replica.rrfs_bufr import make_decoder
    dec, q = make_decoder(sample_path=sample_path)
    _WK["dec"], _WK["q"], _WK["cfg"] = dec, q, cfg
    _WK["s3"] = boto3.client("s3", config=Config(signature_version=UNSIGNED))


def _pp_process(item):
    from bufkit_replica.rrfs_bufr import decode_to_bufkit
    from bufkit_replica.writer import write_buf
    stid, fname = item
    cfg = _WK["cfg"]
    out_path = os.path.join(cfg["outdir"], f"rrfs_{stid.lower()}.buf")
    # NOTE: do NOT skip on out_path existing - the filename is the same every cycle,
    # so a stale file from a prior cycle would block regeneration. The cycle-level
    # state-file check in main() is the correct "already done this cycle" gate.

    fname = f"{fname}.{cfg['cycle_tag']}"           # re-stamp base with current cycle
    local = os.path.join(cfg["localdir"], fname)
    if not os.path.exists(local):
        try:
            _WK["s3"].download_file(cfg["bucket"], f"{cfg['s3_prefix']}/{fname}", local)
        except Exception as e:
            return f"FAIL download {stid}: {e}"
    try:
        bf, _ = decode_to_bufkit(local, cycle=cfg["cycle_ts"], dec=_WK["dec"], q=_WK["q"])
    except Exception as e:
        return f"FAIL decode {stid}: {e}"
    try:
        write_buf(bf, out_path)
    except Exception as e:
        return f"FAIL write {stid}: {e}"
    n  = len(bf.soundings)
    lv = bf.soundings[0].levels.shape[0] if bf.soundings else 0
    return f"OK {stid}: {n}fhr x {lv}lv"
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
ROOT        = os.path.join(SCRIPT_DIR, "..")
DEFAULT_OUTDIR   = r"C:\Program Files (x86)\BUFKIT\Data"
DEFAULT_LOCALDIR = os.path.join(ROOT, "samples", "rrfs", "cache")
DEFAULT_MAP      = os.path.join(ROOT, "samples", "rrfs", "rpid_map.json")
DEFAULT_STATIONS = os.path.join(ROOT, "bufkit_replica", "stations_ww.txt")
DEFAULT_LOG      = os.path.join(SCRIPT_DIR, "update_rrfs.log")


# ----------------------------------------------------------------------
# Cycle discovery
# ----------------------------------------------------------------------

def latest_available_cycle(s3_client, source: str, member: str,
                           max_age_h: int = 36) -> tuple[str, str] | None:
    """Return (YYYYMMDD, HH) for the most recent RRFS cycle available on S3,
    going back up to max_age_h hours from now.  Returns None if nothing found."""
    now = datetime.now(timezone.utc)
    for delta_days in range(3):
        d = now - timedelta(days=delta_days)
        date_str = d.strftime("%Y%m%d")
        for hh in HOURS:
            cycle_dt = datetime(d.year, d.month, d.day, int(hh), tzinfo=timezone.utc)
            age_h = (now - cycle_dt).total_seconds() / 3600
            if age_h < 0 or age_h > max_age_h:
                continue
            # Quick existence check: does the sample file exist in this prefix?
            prefix = bufr_prefix(source, date_str, hh, member)
            sample_key = f"{prefix}/{SAMPLE_FILE}.{date_str}{hh}"
            try:
                s3_client.head_object(Bucket=BUCKET, Key=sample_key)
                return date_str, hh
            except Exception:
                continue
    return None


# ----------------------------------------------------------------------
# Sample file
# ----------------------------------------------------------------------

def ensure_sample(s3_client, source: str, member: str, date_str: str, hh: str,
                  localdir: str) -> str:
    """Download the BUFR table sample file (bufr.000001) if not cached."""
    fname = f"{SAMPLE_FILE}.{date_str}{hh}"
    local = os.path.join(localdir, fname)
    if not os.path.exists(local):
        key = f"{bufr_prefix(source, date_str, hh, member)}/{fname}"
        logging.info("Downloading sample %s ...", key)
        os.makedirs(localdir, exist_ok=True)
        s3_client.download_file(BUCKET, key, local)
    return local


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Auto-update RRFS BUFKIT profiles")
    ap.add_argument("--outdir",    default=DEFAULT_OUTDIR)
    ap.add_argument("--localdir",  default=DEFAULT_LOCALDIR)
    ap.add_argument("--map",       default=DEFAULT_MAP)
    ap.add_argument("--stations",  default=DEFAULT_STATIONS)
    ap.add_argument("--source",    default="det", choices=["det", "ens"],
                    help="det = deterministic RRFS-A (default); ens = REFS member")
    ap.add_argument("--member",    default=MEMBER, help="ensemble member (ens source only)")
    ap.add_argument("--workers",   type=int, default=6)
    ap.add_argument("--max-age-h", type=int, default=36, dest="max_age_h")
    ap.add_argument("--logfile",   default=DEFAULT_LOG)
    ap.add_argument("--statefile", default=os.path.join(SCRIPT_DIR, "update_rrfs.state"),
                    help="Records the last cycle processed; the job no-ops until a "
                         "newer cycle appears. Lets you schedule frequently without rework.")
    ap.add_argument("--force",     action="store_true")
    ap.add_argument("--publish-repo", default=None, dest="publish_repo",
                    help="HF dataset repo id to upload rrfs_*.buf to after the run "
                         "(e.g. ORG/rrfs-bufkit). Requires `hf auth login`.")
    args = ap.parse_args()

    # Logging: both console and file
    handlers = [logging.StreamHandler(sys.stdout)]
    if args.logfile:
        os.makedirs(os.path.dirname(os.path.abspath(args.logfile)), exist_ok=True)
        handlers.append(logging.FileHandler(args.logfile, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )
    # pybufrkit logs "Configure Section N..." at INFO for every BUFR message -
    # hundreds of lines per file. Silence it: huge log/IO win, faster decode.
    logging.getLogger("pybufrkit").setLevel(logging.WARNING)

    sys.path.insert(0, ROOT)
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config

    s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED))

    # -- 1. Find latest cycle --------------------------------------------------
    logging.info("Scanning S3 for latest RRFS cycle (source=%s, max age %d h) ...",
                 args.source, args.max_age_h)
    result = latest_available_cycle(s3, args.source, args.member, args.max_age_h)
    if result is None:
        logging.error("No RRFS cycle found in the last %d hours. Exiting.", args.max_age_h)
        sys.exit(1)
    date_str, hh = result
    cycle_tag = f"{date_str}{hh}"
    logging.info("Using cycle: %s %sZ", date_str, hh)

    # Skip if this cycle was already processed (lets the job be scheduled often,
    # e.g. hourly, and only do real work the ~4x/day a new cycle lands). --force
    # overrides. The state file records the last cycle we completed.
    if not args.force and args.statefile and os.path.exists(args.statefile):
        last = open(args.statefile).read().strip()
        if last == cycle_tag:
            logging.info("Cycle %sZ already processed (state=%s); nothing to do.",
                         hh, last)
            return

    # -- 2. Ensure sample file (needed for BUFR table extraction) -------------
    os.makedirs(args.localdir, exist_ok=True)
    sample = ensure_sample(s3, args.source, args.member, date_str, hh, args.localdir)
    logging.info("Sample file: %s", sample)

    # -- 3. Build decoder once in main thread ----------------------------------
    from bufkit_replica.rrfs_bufr import make_decoder
    logging.info("Building BUFR table root ...")
    dec, q = make_decoder(sample_path=sample)
    logging.info("Tables ready.")

    # -- 4. Load station map & WW list -----------------------------------------
    if not os.path.exists(args.map):
        logging.error("rpid_map.json not found: %s  -  run scan_rrfs_rpids.py first", args.map)
        sys.exit(1)
    r2f = json.load(open(args.map))["rpid_to_file"]

    # Map values are cycle-stamped (bufr.NNNNNN.YYYYMMDDHH from when the map was
    # built); the NNNNNN station slot is stable, so keep only that base and
    # re-stamp with the current cycle below.
    def base_name(f: str) -> str:
        return ".".join(f.split(".")[:2])

    ww = [s.strip().upper() for s in open(args.stations).read().splitlines() if s.strip()]
    found   = [(s, base_name(r2f[s])) for s in ww if s in r2f]
    missing = [s for s in ww if s not in r2f]
    logging.info("WW stations: %d  |  In RRFS: %d  |  Missing: %d",
                 len(ww), len(found), len(missing))
    if missing:
        logging.info("  Not in RRFS archive: %s", ", ".join(missing))

    os.makedirs(args.outdir, exist_ok=True)

    # -- 5. Download + decode + write (process pool) ---------------------------
    from concurrent.futures import ProcessPoolExecutor

    s3_prefix = bufr_prefix(args.source, date_str, hh, args.member)
    cycle_ts  = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]} {hh}:00"
    cfg = dict(outdir=args.outdir, localdir=args.localdir, s3_prefix=s3_prefix,
               cycle_ts=cycle_ts, cycle_tag=cycle_tag, bucket=BUCKET)

    ok_n, skip_n, fail_n = 0, 0, 0
    total   = len(found)
    t_start = time.time()

    with ProcessPoolExecutor(max_workers=args.workers,
                             initializer=_pp_init, initargs=(sample, cfg)) as pool:
        for i, msg in enumerate(pool.map(_pp_process, found), 1):
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            eta  = int((total - i) / rate) if rate > 0 else 0
            if msg.startswith("OK"):
                ok_n += 1
                logging.info("  [%3d/%d] %s  (eta %ds)", i, total, msg, eta)
            elif msg.startswith("SKIP"):
                skip_n += 1
                logging.info("  [%3d/%d] %s  (eta %ds)", i, total, msg, eta)
            else:
                fail_n += 1
                logging.warning("  [%3d/%d] %s", i, total, msg)

    elapsed = time.time() - t_start
    logging.info("=" * 60)
    logging.info("Cycle %sZ complete in %.0fs  -  OK: %d  Skip: %d  Fail: %d",
                 hh, elapsed, ok_n, skip_n, fail_n)

    out_files = [x for x in os.listdir(args.outdir)
                 if x.startswith("rrfs_") and x.endswith(".buf")]
    logging.info("rrfs_*.buf files in BUFKIT Data: %d", len(out_files))

    # -- 6. Optionally publish to a Hugging Face dataset for downstream users --
    # End users never run Python: they pull these finished .buf files over HTTPS.
    if args.publish_repo:
        try:
            from huggingface_hub import HfApi
            api = HfApi()
            api.create_repo(args.publish_repo, repo_type="dataset", exist_ok=True)
            logging.info("Publishing %d rrfs_*.buf to HF dataset %s ...",
                         len(out_files), args.publish_repo)
            api.upload_folder(
                folder_path=args.outdir,
                repo_id=args.publish_repo,
                repo_type="dataset",
                allow_patterns=["rrfs_*.buf"],
                commit_message=f"RRFS {cycle_tag} ({args.source})",
            )
            # Squash history so the repo never accumulates old cycles. Each cycle
            # rewrites all 166 .buf files; without this, git history would grow
            # ~20-25 MB/cycle. After squashing the repo stays at one commit (~70 MB
            # - just the current snapshot). Old cycles have no value to retain.
            api.super_squash_history(repo_id=args.publish_repo, repo_type="dataset")
            logging.info("Published (history squashed) -> https://huggingface.co/datasets/%s",
                         args.publish_repo)
        except Exception as e:
            logging.error("Publish failed: %s", e)

    # Record the cycle we just processed so a frequently-scheduled job no-ops
    # until the next cycle lands. Only when we actually produced files this run -
    # never mark a cycle "done" if nothing was written (ok_n == 0).
    if args.statefile and ok_n > 0:
        try:
            with open(args.statefile, "w") as f:
                f.write(cycle_tag)
        except Exception as e:
            logging.warning("Could not write state file %s: %s", args.statefile, e)

    # Prune the download cache: keep only this cycle's files so it can't grow
    # unbounded across scheduled runs.
    try:
        pruned = 0
        for fn in os.listdir(args.localdir):
            if fn.startswith("bufr.") and not fn.endswith(cycle_tag):
                os.remove(os.path.join(args.localdir, fn)); pruned += 1
        if pruned:
            logging.info("Pruned %d stale cache files from %s", pruned, args.localdir)
    except Exception as e:
        logging.warning("Cache prune skipped: %s", e)


if __name__ == "__main__":
    main()
