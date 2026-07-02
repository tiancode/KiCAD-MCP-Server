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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from commands.datasheet_manager import DatasheetManager
from utils.platform_helper import PlatformHelper

logger = logging.getLogger("kicad_interface")

# Word-ish runs for description tokenisation (Unicode: keeps "510kΩ", "X7R").
_WORD_RE = re.compile(r"\w+", re.UNICODE)

_OUT_OF_STOCK_WARNING = (
    "No in-stock matches; showing parts that exist but are out of stock "
    "(pass in_stock=false to include these directly)."
)


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

                # UPSERT (not INSERT OR REPLACE): JLCSearch carries no
                # datasheet, but the full JLCPCB dump does — a plain REPLACE
                # would wipe the ~87% of CDN links already in the DB on every
                # re-import. Keep the existing datasheet whenever the incoming
                # row's is blank.
                cursor.execute(
                    """
                    INSERT INTO components (
                        lcsc, category, subcategory, mfr_part, package,
                        solder_joints, manufacturer, library_type, description,
                        datasheet, stock, price_json, last_updated
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lcsc) DO UPDATE SET
                        category=excluded.category,
                        subcategory=excluded.subcategory,
                        mfr_part=excluded.mfr_part,
                        package=excluded.package,
                        solder_joints=excluded.solder_joints,
                        manufacturer=excluded.manufacturer,
                        library_type=excluded.library_type,
                        description=excluded.description,
                        datasheet=CASE
                            WHEN excluded.datasheet IS NOT NULL AND excluded.datasheet != ''
                            THEN excluded.datasheet
                            ELSE components.datasheet
                        END,
                        stock=excluded.stock,
                        price_json=excluded.price_json,
                        last_updated=excluded.last_updated
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
                        part.get("datasheet") or "",  # datasheet (blank from jlcsearch)
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

    @staticmethod
    def _quote_fts_term(term: str) -> str:
        """Build a safe FTS5 prefix phrase ``"term"*`` from a raw search word.

        Quoting makes the word a literal phrase, so FTS5 query metacharacters
        ('.', '@', '~', parentheses) and tokenizer splits ('4.7uF' -> '4',
        '7uf') can't raise a syntax error or be misread as operators. This is
        the fix for value+unit queries silently returning nothing: an unquoted
        ``4.7uF*`` is a hard FTS5 syntax error ("syntax error near .") that
        ``_run_fts_query`` swallowed into an empty result. A trailing '*'
        (prefix match) is re-applied outside the quotes, where FTS5 expects it.
        Returns '' for a word with no alphanumeric character (pure punctuation),
        so it is dropped rather than poisoning the whole MATCH.
        """
        term = term.strip().rstrip("*").strip()
        if not term or not any(c.isalnum() for c in term):
            return ""
        escaped = term.replace('"', '""')
        return f'"{escaped}"*'

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

        When an in-stock lookup finds nothing, it retries with the stock
        filter dropped and flags ``out_of_stock_only`` if matches exist that
        are simply out of stock — so a real catalogued MPN is reported as
        "exists but out of stock" instead of an ambiguous empty result that
        can't be told apart from "JLC doesn't carry it".
        """
        base_extra: List[str] = []
        base_params: List[Any] = []
        if package:
            base_extra.append("AND package LIKE ?")
            base_params.append(f"%{package}%")
        if library_type:
            base_extra.append("AND library_type = ?")
            base_params.append(library_type)

        def _lookup(apply_stock: bool) -> Tuple[List[Dict], str]:
            extra = list(base_extra)
            if apply_stock:
                extra.append("AND stock > 0")
            tail = " ".join(extra)
            cursor = self.conn.cursor()
            cursor.execute(
                f"SELECT * FROM components WHERE mfr_part = ? {tail} LIMIT ?",
                [mpn, *base_params, limit],
            )
            found = [dict(r) for r in cursor.fetchall()]
            if found:
                return found, "mpn_exact"
            cursor.execute(
                f"SELECT * FROM components WHERE mfr_part LIKE ? {tail} LIMIT ?",
                [f"{mpn}%", *base_params, limit],
            )
            found = [dict(r) for r in cursor.fetchall()]
            return found, ("mpn_prefix" if found else "mpn_none")

        rows, match_mode = _lookup(in_stock)
        out_of_stock_only = False
        if not rows and in_stock:
            rows, match_mode = _lookup(False)
            out_of_stock_only = bool(rows)

        warnings: List[str] = []
        if match_mode == "mpn_prefix":
            warnings.append(
                f"No exact MPN match for '{mpn}'; showing parts whose MPN starts with it."
            )
        if out_of_stock_only:
            warnings.append(
                f"'{mpn}' is in the catalog but every match is out of stock — pass "
                "in_stock=false to include out-of-stock parts in searches."
            )
        return {
            "parts": rows,
            "count": len(rows),
            "match_mode": match_mode,
            "fuzzy": match_mode == "mpn_prefix",
            "out_of_stock_only": out_of_stock_only,
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

        Returns a dict
        ``{parts, count, match_mode, fuzzy, out_of_stock_only, warnings}``:

        - ``mpn`` (if given) takes priority and routes to an exact/prefix
          lookup on the manufacturer part number — the most reliable path.
        - Otherwise free text is matched against the FTS index. Each term is
          quoted as an FTS5 prefix phrase, so value/unit words ('4.7uF',
          '0.5A') don't blow up the query. Terms are first combined with AND
          (precise); if that yields nothing and there is more than one term,
          it retries with OR and flags ``fuzzy``, so a descriptive query
          degrades to best-effort instead of returning empty.
        - When ``in_stock`` is set and a search finds nothing, it retries with
          the stock filter dropped; if out-of-stock matches exist they are
          returned with ``out_of_stock_only=True``, so an empty result is
          known to mean "not in catalog" rather than "all out of stock".
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

        # Keep the stock filter out of structured_filters so it can be dropped
        # on a retry: an in-stock search that finds nothing is re-run without
        # it, and any hits are flagged out_of_stock_only (Fix A) — an empty
        # in-stock result is then known to mean "not in catalog", not "all out
        # of stock".
        out_of_stock_only = False

        def _run(fts_match: Optional[str]) -> List[Dict]:
            nonlocal out_of_stock_only
            stocked = structured_filters + (["c.stock > 0"] if in_stock else [])

            def _exec(filters: List[str]) -> List[Dict]:
                if fts_match is None:
                    return self._run_structured_query(filters, structured_params, limit)
                return self._run_fts_query(fts_match, filters, structured_params, limit)

            rows = _exec(stocked)
            if not rows and in_stock:
                rows = _exec(structured_filters)
                if rows:
                    out_of_stock_only = True
            return rows

        # No text to match — pure filter query.
        if not fts_terms:
            parts = _run(None)
            if out_of_stock_only:
                warnings.append(_OUT_OF_STOCK_WARNING)
            return {
                "parts": parts,
                "count": len(parts),
                "match_mode": "filter",
                "fuzzy": False,
                "out_of_stock_only": out_of_stock_only,
                "warnings": warnings,
            }

        # Quote each term so value/unit words ('4.7uF', '0.5A', '510kΩ') and
        # punctuation can't raise an FTS5 syntax error or be parsed as
        # operators (Fix B). Drop pure-punctuation tokens.
        prefixed = [q for q in (self._quote_fts_term(t) for t in fts_terms) if q]

        # Every token was noise — degrade to a filter-only query.
        if not prefixed:
            parts = _run(None)
            return {
                "parts": parts,
                "count": len(parts),
                "match_mode": "filter",
                "fuzzy": False,
                "out_of_stock_only": out_of_stock_only,
                "warnings": warnings,
            }

        # Precise pass: all terms must match.
        parts = _run(" ".join(prefixed))
        match_mode = "and"
        fuzzy = False

        # Graceful fallback: any term may match, ranked by relevance.
        if not parts and len(prefixed) > 1:
            parts = _run(" OR ".join(prefixed))
            match_mode = "or"
            fuzzy = True
            if parts:
                warnings.append(
                    "No exact (all-terms) match; showing best partial matches ranked by "
                    "relevance. Narrow with package/library_type, or pass a candidate MPN via "
                    "the 'mpn' parameter for an exact lookup."
                )

        if out_of_stock_only:
            warnings.append(_OUT_OF_STOCK_WARNING)

        return {
            "parts": parts,
            "count": len(parts),
            "match_mode": match_mode,
            "fuzzy": fuzzy,
            "out_of_stock_only": out_of_stock_only,
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

    def download_datasheet(
        self,
        lcsc_number: str,
        output_dir: Optional[str] = None,
        overwrite: bool = False,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Download a part's datasheet PDF to disk.

        Resolves the URL from the local DB's ``datasheet`` column first — the
        JLCPCB CDN direct link present for ~87% of parts in the full dump —
        and falls back to the constructed LCSC URL
        (``https://www.lcsc.com/datasheet/<lcsc>.pdf``) when the column is
        blank. Streams the body to ``<output_dir>/<lcsc>.pdf`` (default
        ``<data-dir>/datasheets/``) and verifies the ``%PDF`` magic bytes
        before keeping the file, so an HTML error page or dead link fails
        loudly instead of leaving a bogus ``.pdf``.

        Args:
            lcsc_number: LCSC part number (``C25804``, ``25804`` or ``c25804``).
            output_dir: Destination directory; created if missing.
            overwrite: Re-download even if a non-empty file already exists.
            timeout: Per-request network timeout in seconds.

        Returns:
            ``{"success": True, "lcsc", "path", "url", "bytes", "source"}``
            where ``source`` is ``db`` / ``lcsc_fallback`` / ``cached``, or
            ``{"success": False, "message", ...}`` on any failure.
        """
        norm = DatasheetManager._normalize_lcsc(lcsc_number)
        if not norm:
            return {"success": False, "message": f"Invalid LCSC number: {lcsc_number}"}

        # Prefer the stored CDN link; fall back to the constructed LCSC URL.
        part = self.get_part_info(norm)
        url = (part or {}).get("datasheet") or ""
        source = "db"
        if not url:
            url = DatasheetManager().get_datasheet_url(norm) or ""
            source = "lcsc_fallback"
        if not url:
            return {"success": False, "message": f"No datasheet URL available for {norm}"}

        dest_dir = Path(output_dir) if output_dir else PlatformHelper.get_data_dir() / "datasheets"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{norm}.pdf"

        if dest.exists() and dest.stat().st_size > 0 and not overwrite:
            return {
                "success": True,
                "lcsc": norm,
                "path": str(dest),
                "url": url,
                "bytes": dest.stat().st_size,
                "source": "cached",
            }

        # Stream to a sidecar temp file so a failed download never clobbers a
        # previously-good PDF; promote it only after the magic-byte check.
        tmp = dest.with_name(dest.name + ".part")
        try:
            with requests.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
        except requests.exceptions.RequestException as e:
            tmp.unlink(missing_ok=True)
            return {"success": False, "message": f"Download failed for {norm}: {e}", "url": url}

        with open(tmp, "rb") as fh:
            head = fh.read(5)
        if not head.startswith(b"%PDF"):
            tmp.unlink(missing_ok=True)
            return {
                "success": False,
                "message": (
                    f"Downloaded file for {norm} is not a PDF (got {head!r}); "
                    "the datasheet link may be dead or region-blocked."
                ),
                "url": url,
            }

        tmp.replace(dest)
        return {
            "success": True,
            "lcsc": norm,
            "path": str(dest),
            "url": url,
            "bytes": dest.stat().st_size,
            "source": source,
        }

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
