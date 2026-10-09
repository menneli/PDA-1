import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CSV_FILES = [
    "cian_flat_sale_1_100_sankt-peterburg_08_Oct_2026_22_10_47_998754.csv",
]

DB_PATH = "cian.db"

MARK_MISSING_INACTIVE = False

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS listings (
    id                   INTEGER PRIMARY KEY,   
    url                  TEXT NOT NULL,          
    author               TEXT,
    author_type          TEXT,
    location             TEXT,
    deal_type            TEXT,
    accommodation_type   TEXT,
    floor                INTEGER,
    floors_count         INTEGER,
    rooms_count          INTEGER,        
    total_meters         REAL,
    price                INTEGER,                      
    price_per_m2         REAL GENERATED ALWAYS AS (
                             CASE WHEN total_meters > 0 THEN price / total_meters END
                         ) VIRTUAL,
    district             TEXT,
    street               TEXT,
    house_number         TEXT,
    underground          TEXT,
    residential_complex  TEXT,
    is_active            INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    CHECK (floor IS NULL OR floors_count IS NULL OR floor <= floors_count)
);


CREATE TABLE IF NOT EXISTS import_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file     TEXT NOT NULL,
    snapshot_at     TEXT NOT NULL,
    imported_at     TEXT NOT NULL,
    rows_in_file    INTEGER NOT NULL,
    unique_listings INTEGER NOT NULL,
    inserted        INTEGER NOT NULL,
    updated         INTEGER NOT NULL,
    price_changes   INTEGER NOT NULL,
    deactivated     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_district   ON listings(district);
CREATE INDEX IF NOT EXISTS idx_listings_price      ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_rooms      ON listings(rooms_count);
CREATE INDEX IF NOT EXISTS idx_listings_underground ON listings(underground);
CREATE INDEX IF NOT EXISTS idx_listings_active     ON listings(is_active);
"""

ID_RE = re.compile(r"/flat/(\d+)")
FILENAME_TS_RE = re.compile(r"(\d{2}_[A-Za-z]{3}_\d{4}_\d{2}_\d{2}_\d{2})")


def clean(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


def to_int(value):
    value = clean(value)
    return int(float(value)) if value is not None else None


def to_float(value):
    value = clean(value)
    return float(value) if value is not None else None


def snapshot_time(path: Path) -> str:
    m = FILENAME_TS_RE.search(path.name)
    if m:
        dt = datetime.strptime(m.group(1), "%d_%b_%Y_%H_%M_%S")
    else:
        dt = datetime.fromtimestamp(path.stat().st_mtime)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def read_csv(path: Path):
    records, total = {}, 0
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        for row in reader:
            total += 1
            m = ID_RE.search(row.get("url") or "")
            if not m:
                print(f"  ! skipping row {total}: no listing id in url", file=sys.stderr)
                continue
            lid = int(m.group(1))
            records[lid] = {
                "id": lid,
                "url": row["url"].split("?")[0],
                "author": clean(row.get("author")),
                "author_type": clean(row.get("author_type")),
                "location": clean(row.get("location")),
                "deal_type": clean(row.get("deal_type")),
                "accommodation_type": clean(row.get("accommodation_type")),
                "floor": to_int(row.get("floor")),
                "floors_count": to_int(row.get("floors_count")),
                "rooms_count": to_int(row.get("rooms_count")),
                "total_meters": to_float(row.get("total_meters")),
                "price": to_int(row.get("price")),
                "district": clean(row.get("district")),
                "street": clean(row.get("street")),
                "house_number": clean(row.get("house_number")),
                "underground": clean(row.get("underground")),
                "residential_complex": clean(row.get("residential_complex")),
            }
    return total, records


UPSERT = """
INSERT INTO listings (
    id, url, author, author_type, location, deal_type, accommodation_type,
    floor, floors_count, rooms_count, total_meters, price, district, street,
    house_number, underground, residential_complex,
    is_active
) VALUES (
    :id, :url, :author, :author_type, :location, :deal_type, :accommodation_type,
    :floor, :floors_count, :rooms_count, :total_meters, :price, :district, :street,
    :house_number, :underground, :residential_complex,
    1
)
ON CONFLICT(id) DO UPDATE SET
    url                 = excluded.url,
    author              = excluded.author,
    author_type         = excluded.author_type,
    location            = excluded.location,
    deal_type           = excluded.deal_type,
    accommodation_type  = excluded.accommodation_type,
    floor               = excluded.floor,
    floors_count        = excluded.floors_count,
    rooms_count         = excluded.rooms_count,
    total_meters        = excluded.total_meters,
    price               = COALESCE(excluded.price, listings.price), 
    district            = excluded.district,
    street              = excluded.street,
    house_number        = excluded.house_number,
    underground         = excluded.underground,
    residential_complex = excluded.residential_complex,
    is_active           = 1;
"""


def import_file(conn, path: Path, mark_missing_inactive: bool):
    seen = snapshot_time(path)
    total, records = read_csv(path)

    existing = dict(conn.execute("SELECT id, price FROM listings"))
    inserted = updated = price_changes = 0

    for rec in records.values():
        rec["seen"] = seen
        is_new = rec["id"] not in existing
        old_price = existing.get(rec["id"])
        conn.execute(UPSERT, rec)

        if is_new:
            inserted += 1
        else:
            updated += 1

    deactivated = 0
    if mark_missing_inactive:
        ids = list(records)
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen (id INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM _seen")
        conn.executemany("INSERT INTO _seen VALUES (?)", [(i,) for i in ids])
        cur = conn.execute(
            "UPDATE listings SET is_active = 0 WHERE is_active = 1 AND id NOT IN (SELECT id FROM _seen)"
        )
        deactivated = cur.rowcount

    conn.execute(
        """INSERT INTO import_log (source_file, snapshot_at, imported_at, rows_in_file,
               unique_listings, inserted, updated, price_changes, deactivated)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (path.name, seen, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         total, len(records), inserted, updated, price_changes, deactivated),
    )
    return total, len(records), inserted, updated, price_changes, deactivated


def resolve(p, base):
    p = Path(p)
    return p if p.is_absolute() else base / p


def main():
    base = Path(__file__).resolve().parent
    if CSV_FILES:
        files = [resolve(f, base) for f in CSV_FILES]
    else:
        files = sorted(base.glob("*.csv"))
    missing = [f for f in files if not f.is_file()]
    if missing or not files:
        for f in missing:
            print(f"File not found: {f}", file=sys.stderr)
        if not files:
            print(f"No .csv files found in {base}", file=sys.stderr)
        sys.exit(1)

    files.sort(key=snapshot_time)

    conn = sqlite3.connect(resolve(DB_PATH, base))
    try:
        conn.executescript(SCHEMA)
        with conn:
            for path in files:
                print(f"Importing {path.name}")
                t, u, i, up, pc, d = import_file(conn, path, MARK_MISSING_INACTIVE)
                print(f"  rows in file: {t}, unique listings: {u}")
                print(f"  inserted: {i}, updated: {up}, price changes: {pc}, deactivated: {d}")
    finally:
        conn.close()
    print(f"Done -> {resolve(DB_PATH, base)}")


if __name__ == "__main__":
    main()