# RRFS to BUFKIT

Converts RRFS forecast soundings (NCEP BUFR on the public S3 bucket
`noaa-rrfs-pds`) into BUFKIT `.buf` files and publishes them for download.
This work was compiled by various others who have attempted something like 
this in the past. Everything here is free for public use. Please cite accordingly.

One machine (the "producer") builds and publishes the files. Everyone else runs a
small Perl script that downloads the finished `.buf` files into BUFKIT. No Python
on the consumer side.

## Layout

```
code/
  bufkit_replica/      .buf read/write library + RRFS BUFR decoder
  tools/               command-line entry points
  samples/             sample inputs + reference .buf files
  tests/               round-trip test
  requirements.txt     Python dependencies
scripts/               operator/consumer scripts (.bat, .pl)
```

## What each file does

| File | Purpose |
|---|---|
| `code/tools/update_rrfs.py` | Main entry point. Finds the latest cycle on S3, downloads the station BUFR files, decodes them, writes `rrfs_*.buf`, and optionally publishes. The only script you schedule. |
| `code/tools/scan_rrfs_rpids.py` | Rebuilds `rpid_map.json` (station ID to BUFR file index). Run only if NCEP renumbers stations. |
| `code/bufkit_replica/rrfs_bufr.py` | Decodes NCEP class-1 BUFR into in-memory soundings; computes height and the convective index block. |
| `code/bufkit_replica/writer.py` | Writes the in-memory soundings to BUFKIT `.buf` text. Format-critical (see Constraints). |
| `code/bufkit_replica/parser.py` | Reads a `.buf` file back into objects. Used by the round-trip test. |
| `code/bufkit_replica/grib_to_buf.py` | Field-name/parameter tables imported by `rrfs_bufr.py`. Required dependency. |
| `code/bufkit_replica/__init__.py` | Package init; exposes the parser. |
| `code/bufkit_replica/stations_ww.txt` | Station list to produce (174 listed; 168 exist in the RRFS domain). |
| `code/samples/rrfs/rpid_map.json` | Station ID to `bufr.NNNNNN` mapping used by `update_rrfs.py`. |
| `code/samples/rrfs/bufr.*` | Two sample BUFR files (table extraction + a decode smoke test). |
| `code/samples/*.buf` | Reference `.buf` files from other models, for byte-level comparison. |
| `code/tests/roundtrip.py` | Parses a `.buf`, re-writes it, and checks they match. |
| `scripts/RRFS Update Dataset.bat` | Runs the producer. Edit the two paths and the publish target at the top. |
| `scripts/WW Bufkit RRFS.pl` | Consumer downloader. Pulls the published `.buf` files into the BUFKIT Data folder. Set `$REPO` to the host. |
| `scripts/Setup RRFS in BUFKIT.pl` | Run once per machine. Adds an `RRFS` line to the BUFKIT model menu. |

## Requirements

Python 3.11. Install eccodes via conda, then the rest with pip:

```
conda install -c conda-forge eccodes python-eccodes
pip install -r code/requirements.txt
```

## Run the producer

```
python code/tools/update_rrfs.py --source det --publish-repo ORG/rrfs-bufkit
```

Finds the latest cycle; if it is new, downloads all stations, decodes them, writes
`rrfs_*.buf` to the output directory, publishes, and exits. If the cycle was
already processed it exits in about a second. Schedule it as often as you like.

| Flag | Meaning |
|---|---|
| `--source det` | Deterministic RRFS-A (default). `ens` = an ensemble member. |
| `--publish-repo <id>` | Hugging Face dataset to upload to. Omit to write local files only. |
| `--outdir <dir>` | Output directory (default `C:\Program Files (x86)\BUFKIT\Data`). |
| `--force` | Reprocess even if the cycle was already done. |
| `--workers N` | Parallel processes (default 6). |

Publishing requires `hf auth login`. The producer squashes the dataset history
each run, so storage stays flat.

## Consumers

- `scripts/Setup RRFS in BUFKIT.pl` - run once to add RRFS to the BUFKIT menu.
- `scripts/WW Bufkit RRFS.pl` - run each cycle to download the latest `.buf` files.
  Set `$REPO` to wherever the producer publishes. Needs only BUFKIT, Perl, and curl.

## Constraints (do not break these or BUFKIT will crash)

The writer and index code enforce these. They are not optional.

1. `.buf` files must use CRLF (`\r\n`) line endings.
2. `STIM` increments per sounding (0, 1, 2, ...).
3. Every level must satisfy `Td <= Tw <= T`.
4. `CFRL` (cloud fraction) must be populated on every level, never all-missing.
5. `LCLT` is written in Kelvin, not Celsius.
6. `LFCT` and `EQLV` are paired - emit both or neither.

When changing the writer or index block, re-test the output in BUFKIT.

## Known gaps

- `SWET` and `BRCH` indices are computed; if reworking them, match GEMPAK's
  definitions and units exactly.
- `rpid_map.json` is a static snapshot; rebuild with `scan_rrfs_rpids.py` if NCEP
  renumbers stations.
