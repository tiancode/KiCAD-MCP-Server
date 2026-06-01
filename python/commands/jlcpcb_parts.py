"""
JLCPCB Parts Database Manager

Manages local SQLite database of JLCPCB parts for fast searching
and component selection.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.platform_helper import PlatformHelper

logger = logging.getLogger("kicad_interface")

# Word-ish runs for description tokenisation (Unicode: keeps "510kΩ", "X7R").
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class JLCPCBPartsManager:
    """
    Manages local database of JLCPCB parts

    Provides fast parametric search, filtering, and package-to-footprint mapping.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize parts database manager

        Args:
            db_path: Path to SQLite database file (default: platform-specific
                user data directory, e.g. ~/.local/share/kicad-mcp/jlcpcb_parts.db
                on Linux). See PlatformHelper.get_data_dir() for platform paths.
        """
        if db_path is None:
            data_dir = PlatformHelper.get_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(data_dir / "jlcpcb_parts.db")

        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        # Cache of "does this column hold any non-empty value" probes. The
        # JLCSearch-sourced DB leaves category/subcategory/manufacturer empty,
        # so filters on them would silently match nothing; we probe once.
        self._column_has_data_cache: Dict[str, bool] = {}
        self._init_database()

    def _init_database(self) -> None:
        """Initialize SQLite database with schema"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row  # Return rows as dicts

        cursor = self.conn.cursor()

        # Create components table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS components (
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

        # Create indexes for fast searching
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_category ON components(category, subcategory)"
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_package ON components(package)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_manufacturer ON components(manufacturer)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_library_type ON components(library_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mfr_part ON components(mfr_part)")

        # Full-text search index for descriptions
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS components_fts USING fts5(
                lcsc,
                description,
                mfr_part,
                manufacturer,
                content=components
            )
        """)

        self.conn.commit()
        logger.info(f"Initialized JLCPCB parts database at {self.db_path}")

    @staticmethod
    def normalize_price_breaks(raw: Any) -> List[Dict[str, Any]]:
        """Coerce any stored/received price shape into clean ``{qty, price}`` tiers.

        Handles, idempotently:
        - a JSON string of any shape below;
        - the JLCSearch tier array ``[{"qFrom":1,"qTo":49,"price":0.396}, ...]``;
        - the legacy double-encoded shape this DB shipped with —
          ``[{"qty":1,"price":"<json array string>"}]`` (the importer used to
          wrap the API's already-JSON price string a second time);
        - a bare number / numeric string (single tier).

        Returns tiers sorted ascending by qty, so ``breaks[0]["price"]`` is the
        unit price. Unparseable input yields ``[]``.
        """
        if isinstance(raw, str):
            s = raw.strip()
            if s[:1] in ("[", "{"):
                try:
                    raw = json.loads(s)
                except json.JSONDecodeError:
                    pass

        if isinstance(raw, bool):  # guard: bool is an int subclass
            return []
        if isinstance(raw, (int, float)):
            return [{"qty": 1, "price": float(raw)}]
        if isinstance(raw, str):
            try:
                return [{"qty": 1, "price": float(raw)}]
            except ValueError:
                return []
        if not isinstance(raw, list):
            return []

        # Legacy double-encoded: a single wrapper whose "price" is itself a
        # JSON string of the real tiers. Unwrap recursively.
        if len(raw) == 1 and isinstance(raw[0], dict) and isinstance(raw[0].get("price"), str):
            inner = JLCPCBPartsManager.normalize_price_breaks(raw[0]["price"])
            if inner:
                return inner

        tiers: List[Dict[str, Any]] = []
        for b in raw:
            if not isinstance(b, dict):
                continue
            try:
                price = float(b.get("price"))
            except (TypeError, ValueError):
                continue
            try:
                qty = int(b.get("qty", b.get("qFrom", 1)))
            except (TypeError, ValueError):
                qty = 1
            tiers.append({"qty": qty, "price": price})
        tiers.sort(key=lambda t: t["qty"])
        return tiers

    def import_jlcsearch_parts(
        self, parts: List[Dict], progress_callback: Optional[Callable[..., Any]] = None
    ) -> None:
        """
        Import parts into database from JLCSearch API response

        Args:
            parts: List of part dicts from JLCSearch API
            progress_callback: Optional callback(current, total, message)
        """
        cursor = self.conn.cursor()
        imported = 0
        skipped = 0

        for i, part in enumerate(parts):
            try:
                # JLCSearch format is different from official API
                # LCSC is an integer, we need to add 'C' prefix
                lcsc = part.get("lcsc")
                if isinstance(lcsc, int):
                    lcsc = f"C{lcsc}"

                # Normalize the API price (already a JSON tier string) into
                # clean {qty, price} tiers — do NOT re-wrap it (that caused the
                # double-encoded price_json this DB shipped with).
                price_breaks = self.normalize_price_breaks(part.get("price") or part.get("price1"))
                price_json = json.dumps(price_breaks)

                # Determine library type from is_basic flag
                library_type = "Basic" if part.get("is_basic") else "Extended"
                if part.get("is_preferred"):
                    library_type = "Preferred"

                # Extract description from various fields
                description_parts = []
                if "resistance" in part:
                    description_parts.append(f"{part['resistance']}Ω")
                if "capacitance" in part:
                    description_parts.append(f"{part['capacitance']}F")
                if "tolerance_fraction" in part:
                    tol = part["tolerance_fraction"] * 100
                    description_parts.append(f"±{tol}%")
                if "power_watts" in part:
                    description_parts.append(f"{part['power_watts']}mW")
                if "voltage" in part:
                    description_parts.append(f"{part['voltage']}V")

                description = part.get("description", " ".join(description_parts))

                cursor.execute(
                    """
                    INSERT OR REPLACE INTO components (
                        lcsc, category, subcategory, mfr_part, package,
                        solder_joints, manufacturer, library_type, description,
                        datasheet, stock, price_json, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        lcsc,  # lcsc with C prefix
                        part.get("category", ""),  # category
                        part.get("subcategory", ""),  # subcategory
                        part.get("mfr", ""),  # mfr_part
                        part.get("package", ""),  # package
                        0,  # solder_joints (not in jlcsearch)
                        part.get("manufacturer", ""),  # manufacturer
                        library_type,  # library_type
                        description,  # description
                        "",  # datasheet (not in jlcsearch)
                        part.get("stock", 0),  # stock
                        price_json,  # price_json
                        int(datetime.now().timestamp()),  # last_updated
                    ),
                )

                imported += 1

                if progress_callback and (i + 1) % 1000 == 0:
                    progress_callback(i + 1, len(parts), f"Imported {imported} parts...")

            except Exception as e:
                logger.error(f"Error importing part {part.get('lcsc')}: {e}")
                skipped += 1

        # Update FTS index
        cursor.execute("""
            INSERT INTO components_fts(components_fts)
            VALUES('rebuild')
        """)

        self.conn.commit()
        logger.info(f"Import complete: {imported} parts imported, {skipped} skipped")

    def _column_has_data(self, column: str) -> bool:
        """Return True if *column* holds at least one non-empty value.

        Probes once per process and caches the result. The JLCSearch DB
        leaves category/subcategory/manufacturer blank, so a LIKE filter on
        them matches nothing — callers use this to degrade gracefully instead.
        """
        if column not in self._column_has_data_cache:
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT EXISTS(SELECT 1 FROM components "  # nosec B608 - column is a fixed literal
                f"WHERE {column} IS NOT NULL AND {column} != '')"
            )
            self._column_has_data_cache[column] = bool(cursor.fetchone()[0])
        return self._column_has_data_cache[column]

    def _run_fts_query(
        self,
        fts_match: str,
        structured_filters: List[str],
        structured_params: List[Any],
        limit: int,
    ) -> List[Dict]:
        """Run an FTS match joined back to components, ranked by bm25 relevance."""
        sql = [
            "SELECT c.* FROM components_fts",
            "JOIN components c ON c.rowid = components_fts.rowid",
            "WHERE components_fts MATCH ?",
        ]
        params: List[Any] = [fts_match]
        sql.extend(f"AND {clause}" for clause in structured_filters)
        params.extend(structured_params)
        sql.append("ORDER BY bm25(components_fts) LIMIT ?")
        params.append(limit)
        try:
            cursor = self.conn.cursor()
            cursor.execute(" ".join(sql), params)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"FTS search error: {e}")
            return []

    def _run_structured_query(
        self, structured_filters: List[str], structured_params: List[Any], limit: int
    ) -> List[Dict]:
        """Run a filter-only query (no free text) against components."""
        sql = ["SELECT c.* FROM components c WHERE 1=1"]
        sql.extend(f"AND {clause}" for clause in structured_filters)
        params = list(structured_params)
        sql.append("LIMIT ?")
        params.append(limit)
        try:
            cursor = self.conn.cursor()
            cursor.execute(" ".join(sql), params)
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Structured search error: {e}")
            return []

    def _search_by_mpn(
        self,
        mpn: str,
        package: Optional[str] = None,
        library_type: Optional[str] = None,
        in_stock: bool = True,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Look a part up by manufacturer part number — the reliable path.

        Tries an exact (index-backed, BINARY) match first, then a
        case-insensitive prefix match (``LIKE 'mpn%'`` uses SQLite's default
        case-insensitive LIKE, so a lowercase candidate still resolves).
        """
        extra: List[str] = []
        extra_params: List[Any] = []
        if package:
            extra.append("AND package LIKE ?")
            extra_params.append(f"%{package}%")
        if library_type:
            extra.append("AND library_type = ?")
            extra_params.append(library_type)
        if in_stock:
            extra.append("AND stock > 0")
        tail = " ".join(extra)
        cursor = self.conn.cursor()

        cursor.execute(
            f"SELECT * FROM components WHERE mfr_part = ? {tail} LIMIT ?",
            [mpn, *extra_params, limit],
        )
        rows = [dict(r) for r in cursor.fetchall()]
        if rows:
            return {
                "parts": rows,
                "count": len(rows),
                "match_mode": "mpn_exact",
                "fuzzy": False,
                "warnings": [],
            }

        cursor.execute(
            f"SELECT * FROM components WHERE mfr_part LIKE ? {tail} LIMIT ?",
            [f"{mpn}%", *extra_params, limit],
        )
        rows = [dict(r) for r in cursor.fetchall()]
        warnings: List[str] = []
        if rows:
            warnings.append(
                f"No exact MPN match for '{mpn}'; showing parts whose MPN starts with it."
            )
        return {
            "parts": rows,
            "count": len(rows),
            "match_mode": "mpn_prefix" if rows else "mpn_none",
            "fuzzy": bool(rows),
            "warnings": warnings,
        }

    def search_parts_meta(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        package: Optional[str] = None,
        library_type: Optional[str] = None,
        manufacturer: Optional[str] = None,
        mpn: Optional[str] = None,
        in_stock: bool = True,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Search for parts, returning matches plus match metadata.

        Returns a dict ``{parts, count, match_mode, fuzzy, warnings}``:

        - ``mpn`` (if given) takes priority and routes to an exact/prefix
          lookup on the manufacturer part number — the most reliable path.
        - Otherwise free text is matched against the FTS index. Terms are
          first combined with AND (precise); if that yields nothing and there
          is more than one term, it retries with OR and flags ``fuzzy``, so a
          descriptive query degrades to best-effort instead of returning empty.
        - ``category``/``manufacturer`` are blank in the JLCSearch DB, so when
          those columns hold no data the value is folded into the text search
          and a warning is recorded rather than silently matching nothing.
        """
        # MPN-first: the only consistently reliable lookup.
        if mpn:
            return self._search_by_mpn(
                mpn,
                package=package,
                library_type=library_type,
                in_stock=in_stock,
                limit=limit,
            )

        warnings: List[str] = []
        fts_terms: List[str] = []
        if query:
            fts_terms.extend(query.strip().split())

        structured_filters: List[str] = []
        structured_params: List[Any] = []

        if category:
            if self._column_has_data("category"):
                structured_filters.append("c.category LIKE ?")
                structured_params.append(f"%{category}%")
            else:
                fts_terms.extend(category.split())
                warnings.append(
                    "category is empty in this database (JLCSearch import); folded it into "
                    "the text search instead. Use the 'package' filter or an 'mpn' for precision."
                )
        if manufacturer:
            if self._column_has_data("manufacturer"):
                structured_filters.append("c.manufacturer LIKE ?")
                structured_params.append(f"%{manufacturer}%")
            else:
                fts_terms.extend(manufacturer.split())
                warnings.append(
                    "manufacturer is empty in this database (JLCSearch import); folded it into "
                    "the text search instead."
                )
        if package:
            structured_filters.append("c.package LIKE ?")
            structured_params.append(f"%{package}%")
        if library_type:
            structured_filters.append("c.library_type = ?")
            structured_params.append(library_type)
        if in_stock:
            structured_filters.append("c.stock > 0")

        # No text to match — pure filter query.
        if not fts_terms:
            parts = self._run_structured_query(structured_filters, structured_params, limit)
            return {
                "parts": parts,
                "count": len(parts),
                "match_mode": "filter",
                "fuzzy": False,
                "warnings": warnings,
            }

        prefixed = [t if t.endswith("*") else f"{t}*" for t in fts_terms]

        # Precise pass: all terms must match.
        parts = self._run_fts_query(
            " ".join(prefixed), structured_filters, structured_params, limit
        )
        match_mode = "and"
        fuzzy = False

        # Graceful fallback: any term may match, ranked by relevance.
        if not parts and len(prefixed) > 1:
            parts = self._run_fts_query(
                " OR ".join(prefixed), structured_filters, structured_params, limit
            )
            match_mode = "or"
            fuzzy = True
            if parts:
                warnings.append(
                    "No exact (all-terms) match; showing best partial matches ranked by "
                    "relevance. Narrow with package/library_type, or pass a candidate MPN via "
                    "the 'mpn' parameter for an exact lookup."
                )

        return {
            "parts": parts,
            "count": len(parts),
            "match_mode": match_mode,
            "fuzzy": fuzzy,
            "warnings": warnings,
        }

    def search_parts(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        package: Optional[str] = None,
        library_type: Optional[str] = None,
        manufacturer: Optional[str] = None,
        in_stock: bool = True,
        limit: int = 20,
    ) -> List[Dict]:
        """Search for parts with filters, returning just the list of matches.

        Thin back-compat wrapper over :meth:`search_parts_meta` for callers
        (e.g. ``suggest_alternatives``) that only need the rows.
        """
        return self.search_parts_meta(
            query=query,
            category=category,
            package=package,
            library_type=library_type,
            manufacturer=manufacturer,
            in_stock=in_stock,
            limit=limit,
        )["parts"]

    def get_part_info(self, lcsc_number: str) -> Optional[Dict]:
        """
        Get detailed information for specific LCSC part

        Args:
            lcsc_number: LCSC part number (e.g., "C25804")

        Returns:
            Part info dict or None if not found
        """
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM components WHERE lcsc = ?", (lcsc_number,))
        row = cursor.fetchone()

        if row:
            part = dict(row)
            part["price_breaks"] = self.normalize_price_breaks(part.get("price_json"))
            return part
        return None

    def get_database_stats(self) -> Dict:
        """Get statistics about the database"""
        cursor = self.conn.cursor()

        cursor.execute("SELECT COUNT(*) as total FROM components")
        total = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) as basic FROM components WHERE library_type = 'Basic'")
        basic = cursor.fetchone()["basic"]

        cursor.execute(
            "SELECT COUNT(*) as extended FROM components WHERE library_type = 'Extended'"
        )
        extended = cursor.fetchone()["extended"]

        cursor.execute("SELECT COUNT(*) as in_stock FROM components WHERE stock > 0")
        in_stock = cursor.fetchone()["in_stock"]

        return {
            "total_parts": total,
            "basic_parts": basic,
            "extended_parts": extended,
            "in_stock": in_stock,
            "db_path": self.db_path,
        }

    def map_package_to_footprint(self, package: str) -> List[str]:
        """
        Map JLCPCB package name to KiCAD footprint(s)

        Args:
            package: JLCPCB package name (e.g., "0603", "SOT-23")

        Returns:
            List of possible KiCAD footprint library refs
        """
        # Load mapping from JSON file or use defaults
        mappings = {
            "0402": [
                "Resistor_SMD:R_0402_1005Metric",
                "Capacitor_SMD:C_0402_1005Metric",
                "LED_SMD:LED_0402_1005Metric",
            ],
            "0603": [
                "Resistor_SMD:R_0603_1608Metric",
                "Capacitor_SMD:C_0603_1608Metric",
                "LED_SMD:LED_0603_1608Metric",
            ],
            "0805": ["Resistor_SMD:R_0805_2012Metric", "Capacitor_SMD:C_0805_2012Metric"],
            "1206": ["Resistor_SMD:R_1206_3216Metric", "Capacitor_SMD:C_1206_3216Metric"],
            "SOT-23": ["Package_TO_SOT_SMD:SOT-23", "Package_TO_SOT_SMD:SOT-23-3"],
            "SOT-23-5": ["Package_TO_SOT_SMD:SOT-23-5"],
            "SOT-23-6": ["Package_TO_SOT_SMD:SOT-23-6"],
            "SOIC-8": ["Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"],
            "SOIC-16": ["Package_SO:SOIC-16_3.9x9.9mm_P1.27mm"],
            "QFN-20": ["Package_DFN_QFN:QFN-20-1EP_4x4mm_P0.5mm_EP2.5x2.5mm"],
            "QFN-32": ["Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm_EP3.45x3.45mm"],
        }

        # Normalize package name
        package_normalized = package.strip().upper()

        for key, footprints in mappings.items():
            if key.upper() in package_normalized:
                return footprints

        return []

    def _description_fts_terms(self, description: str, limit: int = 12) -> List[str]:
        """Extract FTS-safe, discriminative tokens from a part description.

        Keeps word tokens that contain at least one letter, so spec/value
        tokens ('510kΩ', '100mW', 'X7R') and type words ('Thick', 'Resistor')
        survive while bare numbers, temperature ranges, and symbol-only
        fragments ('-55℃~+155℃', '±1%') are dropped. bm25 handles IDF, so
        generic words ('ROHS') self-attenuate. Tokens are lowercased and
        de-duplicated, order preserved, capped at *limit*.
        """
        terms: List[str] = []
        for tok in _WORD_RE.findall(description or ""):
            if len(tok) < 2 or tok.isdigit() or not any(c.isalpha() for c in tok):
                continue
            low = tok.lower()
            if low not in terms:
                terms.append(low)
            if len(terms) >= limit:
                break
        return terms

    def suggest_alternatives(self, lcsc_number: str, limit: int = 5) -> List[Dict]:
        """
        Find alternative parts similar to the given LCSC number

        Uses the reference part's description as a relevance query (bm25)
        constrained to the same package + in stock, so functionally-similar
        parts (same value/spec) surface — the category/subcategory columns are
        blank in the JLCSearch DB and can't be used. The bm25 hit set is an
        over-fetched candidate pool; the final pick prioritizes cheaper price,
        higher stock, and Basic library type. Falls back to a package-only
        match if the description yields no usable tokens.

        Args:
            lcsc_number: Reference LCSC part number
            limit: Maximum alternatives to return

        Returns:
            List of alternative parts
        """
        part = self.get_part_info(lcsc_number)
        if not part:
            return []

        package = part.get("package") or ""
        terms = self._description_fts_terms(part.get("description", ""))

        alternatives: List[Dict] = []
        if terms:
            # Quote each token so words like "OR" stay literal, not operators.
            fts_match = " OR ".join(f'"{t}"' for t in terms)
            structured_filters: List[str] = []
            structured_params: List[Any] = []
            if package:
                structured_filters.append("c.package LIKE ?")
                structured_params.append(f"%{package}%")
            structured_filters.append("c.stock > 0")
            # Over-fetch: bm25 gives the similar pool, the re-sort below picks
            # the cheapest Basic part within it.
            alternatives = self._run_fts_query(
                fts_match, structured_filters, structured_params, limit * 6
            )

        # Fallback: no usable description tokens — same package, in stock.
        if not alternatives:
            alternatives = self.search_parts(package=package, in_stock=True, limit=limit * 3)

        # Filter out the original part
        alternatives = [p for p in alternatives if p["lcsc"] != lcsc_number]

        # Sort by: Basic first, then by price, then by stock
        def sort_key(p: Dict[str, Any]) -> Tuple[int, float, int]:
            is_basic = 1 if p.get("library_type") == "Basic" else 0
            breaks = self.normalize_price_breaks(p.get("price_json"))
            price = breaks[0]["price"] if breaks else 999
            stock = p.get("stock", 0)

            return (-is_basic, price, -stock)

        alternatives.sort(key=sort_key)

        return alternatives[:limit]

    def close(self) -> None:
        """Close database connection"""
        if self.conn:
            self.conn.close()


if __name__ == "__main__":
    # Test the parts manager
    logging.basicConfig(level=logging.INFO)

    manager = JLCPCBPartsManager()

    # Get stats
    stats = manager.get_database_stats()
    print(f"\nDatabase Statistics:")
    print(f"  Total parts: {stats['total_parts']}")
    print(f"  Basic parts: {stats['basic_parts']}")
    print(f"  Extended parts: {stats['extended_parts']}")
    print(f"  In stock: {stats['in_stock']}")
    print(f"  Database: {stats['db_path']}")

    if stats["total_parts"] > 0:
        print("\nSearching for '10k resistor'...")
        results = manager.search_parts(query="10k resistor", limit=5)
        for part in results:
            print(
                f"  {part['lcsc']}: {part['mfr_part']} - {part['description']} ({part['library_type']})"
            )
