#!/usr/bin/env python3
"""
Download JLCPCB parts database from yaqwsx/jlcparts pre-built cache.

This downloads the full JLCPCB catalog (~421MB compressed, ~1.5GB SQLite)
from GitHub Pages in ~5 minutes instead of the broken JLCSearch API approach.

The cache.sqlite3 file contains all JLCPCB parts with stock, pricing,
and category data. We then convert it into the format expected by the
KiCad MCP server's JLCPCBPartsManager.

The upstream archive is monolithic (no incremental/delta publishing), so a
"cheap" re-run is impossible once it changes. What this script *can* avoid:

  * Re-runs when upstream hasn't changed: cache.zip's Last-Modified is recorded
    next to the DB; a matching probe exits immediately with zero downloads.
  * Restarting an interrupted download: the volume archives are kept between
    runs and resumed (`curl -C -`); only the bulky extracted SQLite is deleted.
"""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# Anchored at the repo root: scripts/.. → /python
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))
from utils.platform_helper import PlatformHelper  # noqa: E402

DATA_DIR = PlatformHelper.get_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DIR = DATA_DIR / "jlcparts_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = "https://yaqwsx.github.io/jlcparts/data"
MAX_PARTS = 60  # probe up to this many split volumes

TARGET_DB = DATA_DIR / "jlcpcb_parts.db"

# Sidecar recording the upstream cache.zip Last-Modified that the *completed*
# TARGET_DB was built from. A re-run skips entirely while this still matches.
LM_MARKER = DATA_DIR / "jlcpcb_parts.last_modified"
# Snapshot the partially-downloaded volumes in CACHE_DIR belong to. Lets an
# interrupted download resume; a mismatch means the cached volumes are stale.
INPROGRESS_MARKER = CACHE_DIR / ".downloading_lm"


def curl_download(url: str, dest: Path, resume: bool = False) -> bool:
    """Download url to dest with curl. Returns False on 4xx/5xx or network error.

    With resume=True, continues a partial file (`curl -C -`) instead of restarting.
    """
    cmd = ["curl", "-L", "-f", "-o", str(dest), "--progress-bar"]
    if resume:
        cmd += ["-C", "-"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def remote_head(url: str) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """HEAD `url`; return (status_code, content_length, last_modified).

    status_code is the final HTTP status after redirects, or None when curl got
    no response at all (DNS/connection/timeout). The None-vs-number distinction
    lets callers tell a real network error apart from a server 404, so a blip
    mid-enumeration is not mistaken for "no more volumes".
    """
    result = subprocess.run(["curl", "-sI", "-L", url], capture_output=True, text=True)
    if result.returncode != 0:
        return (None, None, None)
    status: Optional[int] = None
    length: Optional[int] = None
    last_modified: Optional[str] = None
    for raw in result.stdout.splitlines():
        line = raw.strip()
        low = line.lower()
        if low.startswith("http/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        elif low.startswith("content-length:"):
            try:
                length = int(line.split(":", 1)[1].strip())
            except ValueError:
                length = None
        elif low.startswith("last-modified:"):
            last_modified = line.split(":", 1)[1].strip()
    return (status, length, last_modified)


def _fetch_one(url: str, dest: Path) -> str:
    """Ensure `dest` is a complete copy of `url`.

    Returns "done", "missing" (server 404 — expected past the last split
    volume), or "error" (network failure or other HTTP error — must NOT be read
    as end-of-volumes).
    """
    status, remote_len, _ = remote_head(url)
    if status is None:
        return "error"  # network error — distinct from a 404
    if status == 404:
        if dest.exists():
            dest.unlink()
        return "missing"
    if not 200 <= status < 300:
        print(f"  {dest.name}: unexpected HTTP {status}")
        return "error"
    if dest.exists() and remote_len is not None:
        local_len = dest.stat().st_size
        if local_len == remote_len:
            print(f"  {dest.name} already complete, skipping")
            return "done"
        if local_len > remote_len:
            print(f"  {dest.name} corrupt (local > remote), re-downloading")
            dest.unlink()
    # Resume only a genuine partial prefix (shorter than the known remote size).
    # If the remote length is unknown we can't tell complete from partial, so we
    # re-download fresh rather than risk `curl -C -` hitting 416 on a full file.
    resume = dest.exists() and remote_len is not None and 0 < dest.stat().st_size < remote_len
    print(f"  {'Resuming' if resume else 'Downloading'} {dest.name}...")
    return "done" if curl_download(url, dest, resume=resume) else "error"


def download_files() -> bool:
    """Download split archive volumes (z01..zNN) then cache.zip.

    Per file: skip when the local copy already matches the remote size, resume a
    short local copy, and re-fetch a too-large (corrupt) one. Volume enumeration
    stops at the first 404 (or MAX_PARTS).
    """
    print("Downloading jlcparts database (~421MB)...")

    for i in range(1, MAX_PARTS + 1):
        part = f"cache.z{i:02d}"
        status = _fetch_one(f"{BASE_URL}/{part}", CACHE_DIR / part)
        if status == "missing":
            print(f"  {part} not found — {i - 1} volumes total")
            break
        if status == "error":
            print(f"  ERROR downloading {part}")
            return False

    if _fetch_one(f"{BASE_URL}/cache.zip", CACHE_DIR / "cache.zip") != "done":
        print("  ERROR downloading cache.zip")
        return False

    return True


def extract_database() -> bool:
    """Extract the split 7z archive to get cache.sqlite3."""
    cache_sqlite = CACHE_DIR / "cache.sqlite3"
    if cache_sqlite.exists() and cache_sqlite.stat().st_size > 100_000_000:
        print(f"cache.sqlite3 already extracted ({cache_sqlite.stat().st_size // (1024*1024)}MB)")
        return True

    print("Extracting archive (requires 7z or p7zip)...")
    # Try 7z first, then 7zz (homebrew)
    for cmd in ["7z", "7zz", "7za"]:
        try:
            result = subprocess.run(
                [cmd, "x", "-y", "-o" + str(CACHE_DIR), str(CACHE_DIR / "cache.zip")],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                print(f"Extracted with {cmd}")
                return True
            else:
                print(f"  {cmd} failed: {result.stderr[:200]}")
        except FileNotFoundError:
            continue

    print("\nERROR: 7z not found. Install with: brew install p7zip")
    return False


def parse_price_breaks(raw: Optional[str]) -> list:
    """Parse jlcparts' price string into MCP price breaks.

    Upstream stores tiered pricing as "1-199:0.0188,200-599:0.0162,..." — each
    segment is "<qtyLow>-<qtyHigh>:<unitPrice>", with an open-ended final tier
    ("20000-:0.01"). We keep {qty: <low>, price: <unit>} per tier.
    """
    breaks: list = []
    if not raw:
        return breaks
    for seg in str(raw).split(","):
        qty_range, sep, price_s = seg.strip().partition(":")
        if not sep:
            continue
        low = qty_range.split("-", 1)[0].strip()
        try:
            breaks.append({"qty": int(low), "price": float(price_s)})
        except ValueError:
            continue
    return breaks


def convert_to_mcp_format() -> bool:
    """Convert jlcparts cache.sqlite3 to the MCP server's expected format."""
    source = CACHE_DIR / "cache.sqlite3"
    if not source.exists():
        print("ERROR: cache.sqlite3 not found")
        return False

    print(f"Reading source database...")
    src = sqlite3.connect(str(source))
    src.row_factory = sqlite3.Row

    # Check schema
    tables = [
        r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    print(f"  Source tables: {tables}")

    # Find the main components table
    comp_table = None
    for t in tables:
        count = src.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
        print(f"    {t}: {count:,} rows")
        if count > 10000 and comp_table is None:
            comp_table = t

    if not comp_table:
        # Try 'components' specifically
        comp_table = "components" if "components" in tables else tables[0]

    # Get column names
    cols = [r[1] for r in src.execute(f"PRAGMA table_info([{comp_table}])").fetchall()]
    print(f"  Using table '{comp_table}' with columns: {cols[:10]}...")

    # Remove old target DB
    if TARGET_DB.exists():
        TARGET_DB.unlink()

    # Create target DB in MCP format
    dst = sqlite3.connect(str(TARGET_DB))
    dst.execute("""
        CREATE TABLE components (
            lcsc TEXT PRIMARY KEY,
            category TEXT,
            subcategory TEXT,
            mfr_part TEXT,
            package TEXT,
            solder_joints INTEGER,
            manufacturer TEXT,
            library_type TEXT,
            description TEXT,
            datasheet TEXT,
            stock INTEGER,
            price_json TEXT,
            last_updated INTEGER
        )
    """)
    dst.execute("CREATE INDEX idx_category ON components(category, subcategory)")
    dst.execute("CREATE INDEX idx_package ON components(package)")
    dst.execute("CREATE INDEX idx_manufacturer ON components(manufacturer)")
    dst.execute("CREATE INDEX idx_library_type ON components(library_type)")
    dst.execute("CREATE INDEX idx_mfr_part ON components(mfr_part)")

    # Map source columns to our schema. Current jlcparts (table `jlc_components`)
    # exposes: lcsc, category, subcategory, mfr, package, joints, manufacturer,
    # library_type ('base'/'expand'), preferred (0/1), stock, price (tiered
    # string), description, datasheet. (Legacy 'basic'/'First Category'/'url'
    # fallbacks are kept for older snapshots.)
    print(f"\nConverting parts to MCP format...")
    now = int(time.time())
    batch = []
    count = 0

    for row in src.execute(f"SELECT * FROM [{comp_table}]"):
        row_dict = dict(row)

        # Adapt column names (jlcparts uses various schemas)
        lcsc = row_dict.get("lcsc") or row_dict.get("LCSC_Part") or row_dict.get("lcsc_id")
        if lcsc is None:
            continue
        if isinstance(lcsc, int):
            lcsc = f"C{lcsc}"
        elif not str(lcsc).startswith("C"):
            lcsc = f"C{lcsc}"

        mfr_part = row_dict.get("mfr") or row_dict.get("MFR_Part") or row_dict.get("mfr_part") or ""
        package = row_dict.get("package") or row_dict.get("Package") or ""
        manufacturer = row_dict.get("manufacturer") or row_dict.get("Manufacturer") or ""
        description = row_dict.get("description") or row_dict.get("Description") or ""
        stock = row_dict.get("stock") or row_dict.get("Stock") or 0
        category = row_dict.get("category") or row_dict.get("First Category") or ""
        subcategory = row_dict.get("subcategory") or row_dict.get("Second Category") or ""
        datasheet = row_dict.get("datasheet") or row_dict.get("url") or ""

        # Library type: upstream uses library_type='base'/'expand' plus a
        # preferred 0/1 flag (the older 'basic'/'is_basic' columns are gone, so
        # the previous mapping classified every part as Extended).
        raw_lib = str(row_dict.get("library_type") or "").strip().lower()
        is_basic = row_dict.get("basic") or row_dict.get("is_basic") or row_dict.get("Basic")
        if raw_lib in ("base", "basic") or is_basic:
            lib_type = "Basic"
        elif row_dict.get("preferred") in (1, "1", True):
            lib_type = "Preferred"
        else:
            lib_type = "Extended"

        solder_joints = row_dict.get("joints") or row_dict.get("solder_joints") or 0
        # Price: upstream is a tiered string ("1-199:0.0188,..."); parse it into
        # real {qty, price} breaks instead of stuffing the raw string into one.
        price_json = json.dumps(parse_price_breaks(row_dict.get("price")))

        batch.append(
            (
                str(lcsc),
                category,
                subcategory,
                mfr_part,
                package,
                int(solder_joints) if solder_joints else 0,
                manufacturer,
                lib_type,
                description,
                datasheet,
                int(stock) if stock else 0,
                price_json,
                now,
            )
        )

        if len(batch) >= 10000:
            dst.executemany(
                """
                INSERT OR REPLACE INTO components
                (lcsc, category, subcategory, mfr_part, package,
                 solder_joints, manufacturer, library_type, description,
                 datasheet, stock, price_json, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                batch,
            )
            count += len(batch)
            batch = []
            if count % 100000 == 0:
                print(f"  Converted {count:,} parts...")

    if batch:
        dst.executemany(
            """
            INSERT OR REPLACE INTO components
            (lcsc, category, subcategory, mfr_part, package,
             solder_joints, manufacturer, library_type, description,
             datasheet, stock, price_json, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            batch,
        )
        count += len(batch)

    # Build FTS index
    print(f"  Building full-text search index...")
    dst.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS components_fts USING fts5(
            lcsc, description, mfr_part, manufacturer,
            content=components
        )
    """)
    dst.execute("INSERT INTO components_fts(components_fts) VALUES('rebuild')")
    dst.commit()

    # Stats
    total = dst.execute("SELECT COUNT(*) FROM components").fetchone()[0]
    basic = dst.execute("SELECT COUNT(*) FROM components WHERE library_type='Basic'").fetchone()[0]
    extended = dst.execute(
        "SELECT COUNT(*) FROM components WHERE library_type='Extended'"
    ).fetchone()[0]

    dst.close()
    src.close()

    # Keep the downloaded volume archives (so an upstream-unchanged re-run stays
    # free and an interrupted one can resume); only the bulky extracted SQLite
    # is disposable.
    print("Cleaning up extracted database (keeping downloaded archives)...")
    if source.exists():
        freed = source.stat().st_size / (1024 * 1024)
        source.unlink()
        print(f"  Removed cache.sqlite3 ({freed:.0f}MB); kept volumes in {CACHE_DIR}")

    db_size = TARGET_DB.stat().st_size / (1024 * 1024)
    print(f"\nDatabase ready: {TARGET_DB}")
    print(f"  Total parts:    {total:,}")
    print(f"  Basic parts:    {basic:,}")
    print(f"  Extended parts: {extended:,}")
    print(f"  DB size:        {db_size:.1f} MB")
    return True


def prepare_cache(upstream_lm: Optional[str]) -> None:
    """Drop cached volumes that don't belong to the current upstream snapshot.

    Volumes are kept (and later resumed) only when the in-progress marker still
    matches `upstream_lm`; otherwise their provenance is stale or unknown.
    """
    inprog = INPROGRESS_MARKER.read_text().strip() if INPROGRESS_MARKER.exists() else ""
    have_volumes = (CACHE_DIR / "cache.zip").exists() or any(CACHE_DIR.glob("cache.z[0-9]*"))
    if have_volumes and inprog != (upstream_lm or ""):
        reason = "different snapshot" if inprog else "unknown provenance"
        print(f"  Discarding stale cached volumes ({reason})")
        for f in CACHE_DIR.iterdir():
            if f.is_file():
                f.unlink()
    # Never trust a leftover extracted SQLite across runs: extract_database's
    # size check can't tell a half-written file from a complete one, and it is
    # cheap to re-extract from the kept volumes. Always start extraction fresh.
    (CACHE_DIR / "cache.sqlite3").unlink(missing_ok=True)
    INPROGRESS_MARKER.write_text(upstream_lm or "")


def main() -> None:
    print("=" * 60)
    print("JLCPCB Parts Database Downloader (jlcparts method)")
    print("=" * 60)
    start = time.time()

    # Cheap freshness probe: the upstream split archive is monolithic (no deltas),
    # so the only "skip" possible is detecting it hasn't changed since we built
    # TARGET_DB. cache.zip's Last-Modified is that snapshot stamp.
    status, _, upstream_lm = remote_head(f"{BASE_URL}/cache.zip")
    if status is None or not 200 <= status < 300:
        print("ERROR: cannot reach upstream cache.zip — check network/URL")
        sys.exit(1)
    print(f"Upstream snapshot: {upstream_lm or 'unknown'}")

    if upstream_lm and TARGET_DB.exists():
        prev = LM_MARKER.read_text().strip() if LM_MARKER.exists() else ""
        if prev == upstream_lm:
            print(f"Local database already matches upstream ({upstream_lm}).")
            print("Nothing to download. Done.")
            return
        if prev:
            print(f"Upstream changed: {prev} -> {upstream_lm}; refreshing.")

    # Reuse cached volumes only if they belong to this snapshot; else discard.
    prepare_cache(upstream_lm)

    if not download_files():
        sys.exit(1)

    if not extract_database():
        sys.exit(1)

    if not convert_to_mcp_format():
        sys.exit(1)

    if upstream_lm:
        LM_MARKER.write_text(upstream_lm)

    elapsed = time.time() - start
    print(f"\nTotal time: {elapsed/60:.1f} minutes")
    print("Done! Restart the MCP server (/mcp) to use the new database.")


if __name__ == "__main__":
    main()
