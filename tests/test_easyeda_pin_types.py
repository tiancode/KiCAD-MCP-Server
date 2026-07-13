"""S7 / F12 regression tests — easyeda2kicad pin-type inference + next hint.

Background
----------
easyeda2kicad emits every pin as electrical type ``unspecified``. ERC then can't
check power driving AND floods pin_to_pin "Unspecified … connected" warnings (the
E2E run saw 35, none clearable through the server). ``import_lcsc_part`` now
post-processes the imported symbol (opt-out ``inferPinTypes``), inferring a type
for EVERY pin from its NAME: power rails → ``power_in``, GPIO/bus → ``bidirectional``,
OUT*/IN* → ``output``/``input``, everything else → ``passive`` (never left
``unspecified``, which is what actually clears the ERC warnings). Any type
easyeda2kicad already set is preserved. It writes atomically and re-validates.
The ``next`` hint was also corrected to the real tool schema
``add_schematic_component(symbol="easyeda:<name>")``.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PYTHON_DIR = Path(__file__).parent.parent / "python"
sys.path.insert(0, str(PYTHON_DIR))

import commands.easyeda_import as ee  # noqa: E402

# A multi-unit part with a power unit (VDD/VSS/VBAT) and a signal unit — mirrors
# the shape of the real GD32F103VET6 cache entry. Manufacturer field carries a
# literal '(' to exercise the quote-aware paren matcher.
_MULTIUNIT_LIB = """\
(kicad_symbol_lib
  (version 20211014)
  (generator https://github.com/uPesy/easyeda2kicad.py)
  (symbol "MCUX"
    (property "Reference" "U" (id 0) (at 0 0 0))
    (property "Value" "MCUX" (id 1) (at 0 0 0))
    (property "Footprint" "easyeda:LQFP" (id 2) (at 0 0 0))
    (property "Manufacturer" "Acme(inc)" (id 4) (at 0 0 0))
    (property "LCSC Part" "C55555" (id 6) (at 0 0 0))
    (symbol "MCUX_1_1"
      (pin unspecified line (at -10 5 0) (length 5)
        (name "PA0" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at -10 0 0) (length 5)
        (name "RESET" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27)))))
    )
    (symbol "MCUX_2_1"
      (pin unspecified line (at 10 5 180) (length 5)
        (name "VDD" (effects (font (size 1.27 1.27))))
        (number "3" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at 10 0 180) (length 5)
        (name "VSS_1" (effects (font (size 1.27 1.27))))
        (number "4" (effects (font (size 1.27 1.27)))))
      (pin unspecified line (at 10 -5 180) (length 5)
        (name "VBAT" (effects (font (size 1.27 1.27))))
        (number "5" (effects (font (size 1.27 1.27)))))
    )
  )
)
"""


def _runner(sym_path, pretty_dir, *, content=_MULTIUNIT_LIB):
    def _run(cmd, timeout):
        sym_path.parent.mkdir(parents=True, exist_ok=True)
        sym_path.write_text(content, encoding="utf-8")
        pretty_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return _run


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    sym = cache / "easyeda.kicad_sym"
    pretty = cache / "easyeda.pretty"
    cfg = tmp_path / "config" / "kicad" / "10.0"
    monkeypatch.setattr(ee, "_CACHE_DIR", cache)
    monkeypatch.setattr(ee, "SYMBOL_LIB_PATH", sym)
    monkeypatch.setattr(ee, "FOOTPRINT_LIB_DIR", pretty)

    def _cfg():
        cfg.mkdir(parents=True, exist_ok=True)
        return cfg

    monkeypatch.setattr(ee, "_resolve_global_config_dir", _cfg)
    return SimpleNamespace(cache=cache, sym=sym, pretty=pretty, cfg=cfg)


def _pin_types(lib_path, symbol_name):
    """Map pin name → electrical type inside a symbol block."""
    content = lib_path.read_text(encoding="utf-8")
    span = ee._symbol_span(content, symbol_name)
    block = content[span[0] : span[1]]
    out = {}
    i = 0
    while True:
        p = block.find("(pin ", i)
        if p == -1:
            break
        end = ee._match_paren(block, p)
        pb = block[p:end]
        hdr = ee._PIN_HEADER_RE.match(pb)
        nm = ee._PIN_NAME_RE.search(pb)
        if hdr and nm:
            out[nm.group(1)] = hdr.group(1)
        i = end
    return out


# ---------------------------------------------------------------------------
# Name classifier
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        ("VDD", True),
        ("VDD_5", True),
        ("VDDA", True),
        ("VCC", True),
        ("VSS", True),
        ("VSSA", True),
        ("GND", True),
        ("GND1", True),
        ("VBAT", True),
        ("vdd", True),  # case-insensitive
        # Families added for S7.
        ("AVDD", True),
        ("AGND", True),
        ("PGND", True),
        ("V3P3", True),
        ("5V0", True),
        ("EP", True),  # exposed pad (exact)
        ("EPAD", True),  # exposed pad (exact)
        ("PA0", False),
        ("RESET", False),
        ("VREF", False),  # V-prefixed but not a rail we retype
        ("VSYNC", False),
        ("EPWM", False),  # EP-prefixed signal must NOT match (exact-only)
        ("", False),
    ],
)
def test_is_power_pin_name(name, expected):
    assert ee._is_power_pin_name(name) is expected


# ---------------------------------------------------------------------------
# Full name → type classifier (one case per heuristic class)
# ---------------------------------------------------------------------------
@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected",
    [
        # power rails
        ("VDD", "power_in"),
        ("VDDA", "power_in"),
        ("AVDD", "power_in"),
        ("V3P3", "power_in"),
        ("5V0", "power_in"),
        ("VSS", "power_in"),
        ("GND", "power_in"),
        ("AGND", "power_in"),
        ("PGND", "power_in"),
        ("EP", "power_in"),
        ("EPAD", "power_in"),
        # GPIO / bus → bidirectional
        ("PA0", "bidirectional"),
        ("PB12", "bidirectional"),
        ("PC13", "bidirectional"),
        ("SDA", "bidirectional"),
        ("SCL", "bidirectional"),
        ("MISO", "bidirectional"),
        ("MOSI", "bidirectional"),
        ("SCK", "bidirectional"),
        ("RX", "bidirectional"),
        ("TX", "bidirectional"),
        ("IO0", "bidirectional"),
        # outputs
        ("OUT", "output"),
        ("LOUT", "output"),
        ("ROUT", "output"),
        ("DOUT", "output"),
        ("OUTL+", "output"),
        # inputs
        ("IN", "input"),
        ("INL", "input"),
        ("DIN", "input"),
        ("AIN", "input"),
        ("FMIN", "input"),
        # no-connect (exact)
        ("NC", "no_connect"),
        ("DNC", "no_connect"),
        # fallback: passive (NEVER unspecified)
        ("RESET", "passive"),
        ("MUTE", "passive"),
        ("XTAL1", "passive"),
        ("", "passive"),
        ("NCS", "passive"),  # NC-prefixed real signal is NOT a no-connect
    ],
)
def test_infer_pin_type_from_unspecified(name, expected):
    # Every pin starts life ``unspecified`` from easyeda2kicad.
    assert ee._infer_pin_type(name, "unspecified") == expected


@pytest.mark.unit
def test_infer_pin_type_preserves_existing_correct_type():
    """A type easyeda2kicad already set (not ``unspecified``) is kept, even if
    the name would otherwise be classified differently."""
    # 'VDD' would infer power_in, but a real output type is preserved.
    assert ee._infer_pin_type("VDD", "output") == "output"
    assert ee._infer_pin_type("PA0", "power_in") == "power_in"
    # No-connect names never clobber a real signal type.
    assert ee._infer_pin_type("NC", "input") == "input"


# ---------------------------------------------------------------------------
# In-place retype
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_apply_pin_type_inference_types_every_pin(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_MULTIUNIT_LIB, encoding="utf-8")

    res = ee._apply_pin_type_inference(lib, "MCUX")
    # All 5 pins move off ``unspecified``.
    assert res["changed"] == 5

    types = _pin_types(lib, "MCUX")
    assert types["VDD"] == "power_in"
    assert types["VSS_1"] == "power_in"
    assert types["VBAT"] == "power_in"
    # Signal pins get a real type too — GPIO → bidirectional, else passive.
    assert types["PA0"] == "bidirectional"
    assert types["RESET"] == "passive"
    # Crucially, NOTHING is left ``unspecified`` (that's what clears the ERC noise).
    assert "unspecified" not in types.values()

    # File is still valid s-expression after the rewrite.
    import sexpdata

    sexpdata.loads(lib.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_apply_pin_type_inference_is_idempotent(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_MULTIUNIT_LIB, encoding="utf-8")
    assert ee._apply_pin_type_inference(lib, "MCUX")["changed"] == 5
    # Second pass changes nothing (every pin already has its inferred type).
    assert ee._apply_pin_type_inference(lib, "MCUX")["changed"] == 0


@pytest.mark.unit
def test_apply_pin_type_inference_unknown_symbol(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_MULTIUNIT_LIB, encoding="utf-8")
    assert ee._apply_pin_type_inference(lib, "NOPE")["changed"] == 0


@pytest.mark.unit
def test_count_symbol_units(tmp_path):
    lib = tmp_path / "easyeda.kicad_sym"
    lib.write_text(_MULTIUNIT_LIB, encoding="utf-8")
    assert ee._count_symbol_units(lib, "MCUX") == 2


# ---------------------------------------------------------------------------
# import_lcsc_part integration
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_import_infers_pin_types_by_default(env, monkeypatch):
    monkeypatch.setattr(ee, "_run", _runner(env.sym, env.pretty))
    res = ee.import_lcsc_part("C55555")
    assert res["success"] is True
    assert res["pin_types_inferred"] == 5  # every pin retyped off unspecified
    types = _pin_types(env.sym, "MCUX")
    assert types["VDD"] == "power_in" and types["PA0"] == "bidirectional"
    assert "unspecified" not in types.values()


@pytest.mark.unit
def test_import_infer_pin_types_opt_out(env, monkeypatch):
    monkeypatch.setattr(ee, "_run", _runner(env.sym, env.pretty))
    res = ee.import_lcsc_part("C55555", infer_pin_types=False)
    assert res["pin_types_inferred"] == 0
    types = _pin_types(env.sym, "MCUX")
    assert types["VDD"] == "unspecified"  # left as-is


@pytest.mark.unit
def test_next_hint_uses_real_schema(env, monkeypatch):
    monkeypatch.setattr(ee, "_run", _runner(env.sym, env.pretty))
    res = ee.import_lcsc_part("C55555")
    # Real schema: symbol="easyeda:<name>" — NOT library=/componentName=.
    assert 'symbol="easyeda:MCUX"' in res["next"]
    assert "library=" not in res["next"]
    assert "componentName=" not in res["next"]
    # Multi-unit parts advertise placeAllUnits.
    assert res["units"] == 2
    assert "placeAllUnits" in res["next"]


@pytest.mark.unit
def test_cached_part_heals_pin_types(env, monkeypatch):
    # Pre-seed the cache with the blanket-unspecified library (no network call).
    env.sym.parent.mkdir(parents=True, exist_ok=True)
    env.sym.write_text(_MULTIUNIT_LIB, encoding="utf-8")
    env.pretty.mkdir(parents=True, exist_ok=True)
    called = []
    monkeypatch.setattr(ee, "_run", lambda *a, **k: called.append(1))

    res = ee.import_lcsc_part("C55555")  # cached path
    assert res["already_cached"] is True
    assert called == []  # no fetch
    assert res["pin_types_inferred"] == 5
    assert _pin_types(env.sym, "MCUX")["VDD"] == "power_in"


@pytest.mark.unit
def test_handler_passes_infer_flag(env, monkeypatch):
    from handlers.jlcpcb import handle_import_jlcpcb_symbol

    monkeypatch.setattr(ee, "_run", _runner(env.sym, env.pretty))
    res = handle_import_jlcpcb_symbol(None, {"lcsc_number": "C55555", "inferPinTypes": False})
    assert res["success"] is True
    assert res["pin_types_inferred"] == 0
