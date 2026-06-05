"""Data models and cache constants for the symbol library package.

Split out of the former monolithic commands/library_symbol.py.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SymbolInfo:
    """Information about a symbol in a library"""

    name: str  # Symbol name (without library prefix)
    library: str  # Library nickname
    full_ref: str  # "Library:SymbolName"
    value: str = ""  # Value property
    description: str = ""  # Description property
    footprint: str = ""  # Footprint reference if present
    lcsc_id: str = ""  # LCSC property if present
    manufacturer: str = ""  # Manufacturer property
    mpn: str = ""  # Part/MPN property
    category: str = ""  # Category property
    datasheet: str = ""  # Datasheet URL
    stock: str = ""  # Stock (from JLCPCB libs)
    price: str = ""  # Price (from JLCPCB libs)
    lib_class: str = ""  # Basic/Preferred/Extended
    sim_pins: str = ""  # Sim.Pins pin mapping (e.g. "1=in+ 2=in- 3=vcc 4=vee 5=out")


@dataclass(frozen=True)
class _SearchPlan:
    """Resolved inputs for one symbol search.

    Built by ``SymbolLibraryManager.plan_search`` and consumed by both
    the executor and the response layer so they can't disagree about
    what was searched.  The fields are independent so the response layer
    can distinguish "no library matched the explicit filter" (warn) from
    "inline prefix parsed and used as scope" (just report it).
    """

    name_query: str
    effective_library: Optional[str]
    inline_prefix: Optional[str]
    libraries_searched: List[str] = field(default_factory=list)
    library_filter_matched_nothing: bool = False
