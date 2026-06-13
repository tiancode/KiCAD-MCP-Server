import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from skip import Schematic

logger = logging.getLogger(__name__)

# Import dynamic symbol loader
try:
    from commands.dynamic_symbol_loader import DynamicSymbolLoader

    DYNAMIC_LOADING_AVAILABLE = True
except ImportError:
    logger.warning("Dynamic symbol loader not available - falling back to template-only mode")
    DYNAMIC_LOADING_AVAILABLE = False


class ComponentManager:
    """Manage components in a schematic"""

    # Initialize dynamic loader (class variable, shared across instances)
    _dynamic_loader = None

    @classmethod
    def get_dynamic_loader(cls) -> Any:
        """Get or create dynamic symbol loader instance"""
        if cls._dynamic_loader is None and DYNAMIC_LOADING_AVAILABLE:
            cls._dynamic_loader = DynamicSymbolLoader()
        return cls._dynamic_loader

    # Template symbol references mapping component type to template reference
    TEMPLATE_MAP = {
        # Passives
        "R": "_TEMPLATE_R",
        "C": "_TEMPLATE_C",
        "L": "_TEMPLATE_L",
        "Y": "_TEMPLATE_Y",
        "Crystal": "_TEMPLATE_Y",
        # Semiconductors
        "D": "_TEMPLATE_D",
        "LED": "_TEMPLATE_LED",
        "Q": "_TEMPLATE_Q_NPN",
        "Q_NPN": "_TEMPLATE_Q_NPN",
        "Q_NMOS": "_TEMPLATE_Q_NMOS",
        "MOSFET": "_TEMPLATE_Q_NMOS",
        # ICs
        "U": "_TEMPLATE_U_OPAMP",
        "OpAmp": "_TEMPLATE_U_OPAMP",
        "IC": "_TEMPLATE_U_OPAMP",
        "U_REG": "_TEMPLATE_U_REG",
        "Regulator": "_TEMPLATE_U_REG",
        # Connectors
        "J": "_TEMPLATE_J2",
        "J2": "_TEMPLATE_J2",
        "J4": "_TEMPLATE_J4",
        "Conn_2": "_TEMPLATE_J2",
        "Conn_4": "_TEMPLATE_J4",
        # Misc
        "SW": "_TEMPLATE_SW",
        "Button": "_TEMPLATE_SW",
        "Switch": "_TEMPLATE_SW",
    }

    @classmethod
    def get_or_create_template(
        cls,
        schematic: Schematic,
        comp_type: str,
        library: Optional[str] = None,
        schematic_path: Optional[Path] = None,
    ) -> tuple:
        """
        Get template reference for a component type, creating it dynamically if needed

        Args:
            schematic: Schematic object
            comp_type: Component type (e.g., 'R', 'LED', 'STM32F103C8Tx')
            library: Optional library name (defaults to 'Device' for common types)
            schematic_path: Optional path to schematic file (required for dynamic loading)

        Returns:
            Tuple of (template_ref, needs_reload) where needs_reload indicates if schematic must be reloaded
        """

        # Helper function to check if template exists in schematic
        def template_exists(schematic: Any, template_ref: str) -> bool:
            """Check if template exists by iterating symbols (handles special characters)"""
            for symbol in schematic.symbol:
                if (
                    hasattr(symbol.property, "Reference")
                    and symbol.property.Reference.value == template_ref
                ):
                    return True
            return False

        # 1. Check static template map first
        if comp_type in cls.TEMPLATE_MAP:
            template_ref = cls.TEMPLATE_MAP[comp_type]
            # Verify template exists in schematic
            if template_exists(schematic, template_ref):
                logger.debug(f"Using static template: {template_ref}")
                return (template_ref, False)

        # 2. Check if dynamically loaded template already exists
        # Build potential template reference names
        potential_refs = []
        if library:
            potential_refs.append(f"_TEMPLATE_{library}_{comp_type}")
        potential_refs.append(f"_TEMPLATE_{comp_type}")

        # Check each potential reference
        for template_ref in potential_refs:
            if template_exists(schematic, template_ref):
                logger.debug(f"Found existing template: {template_ref}")
                return (template_ref, False)

        # 3. Try dynamic loading
        if not DYNAMIC_LOADING_AVAILABLE:
            logger.warning(
                f"Component type '{comp_type}' not in static templates and dynamic loading unavailable"
            )
            # Fall back to basic resistor template
            return ("_TEMPLATE_R", False)

        loader = cls.get_dynamic_loader()
        if not loader:
            logger.warning("Dynamic loader unavailable, using fallback template")
            return ("_TEMPLATE_R", False)

        # Check if schematic path is available
        if schematic_path is None:
            logger.warning("Dynamic loading requires schematic file path but none was provided")
            fallback = cls.TEMPLATE_MAP.get(comp_type, "_TEMPLATE_R")
            return (fallback, False)

        # Determine library name
        if library is None:
            # Default library for common component types
            library = "Device"  # Most passives and basic components are in Device library

        try:
            logger.info(f"Attempting dynamic load: {library}:{comp_type} from {schematic_path}")

            # Use dynamic symbol loader to inject symbol and create template
            template_ref = loader.load_symbol_dynamically(schematic_path, library, comp_type)

            logger.info(f"Successfully loaded symbol dynamically. Template ref: {template_ref}")
            # Signal that schematic needs reload to see new template
            return (template_ref, True)

        except Exception as e:
            logger.error(f"Dynamic loading failed: {e}")
            import traceback

            logger.error(traceback.format_exc())
            # Fall back to static template if available
            fallback = cls.TEMPLATE_MAP.get(comp_type, "_TEMPLATE_R")
            return (fallback, False)

    @staticmethod
    def add_component(
        schematic: Schematic, component_def: dict, schematic_path: Optional[Path] = None
    ) -> Any:
        """
        Add a component to the schematic by cloning from template

        Args:
            schematic: Schematic object to add component to
            component_def: Component definition dictionary
            schematic_path: Optional path to schematic file (enables dynamic symbol loading)

        Returns:
            Tuple of (new_symbol, needs_reload) where needs_reload indicates if caller should reload schematic
        """
        try:
            from commands.schematic import SchematicManager

            logger.info(
                f"Adding component: type={component_def.get('type')}, ref={component_def.get('reference')}"
            )
            logger.debug(f"Full component_def: {component_def}")

            # Get component type and determine template
            comp_type = component_def.get("type", "R")
            library = component_def.get("library", None)  # Optional library specification

            # Get template reference (static or dynamic)
            template_ref, needs_reload = ComponentManager.get_or_create_template(
                schematic, comp_type, library, schematic_path
            )

            # If dynamic loading occurred, reload schematic to see new template
            if needs_reload and schematic_path:
                logger.info(f"Reloading schematic after dynamic loading: {schematic_path}")
                schematic = SchematicManager.load_schematic(str(schematic_path))

            # Find template symbol by reference (handles special characters like +)
            template_symbol = None
            for symbol in schematic.symbol:
                if (
                    hasattr(symbol.property, "Reference")
                    and symbol.property.Reference.value == template_ref
                ):
                    template_symbol = symbol
                    break

            if not template_symbol:
                logger.error(
                    f"Template symbol {template_ref} not found in schematic. Available symbols: {[str(s.property.Reference.value) for s in schematic.symbol]}"
                )
                raise ValueError(
                    f"Template symbol {template_ref} not found. The schematic must be created from template_with_symbols.kicad_sch"
                )

            # Clone the template symbol
            new_symbol = template_symbol.clone()
            logger.debug(f"Cloned template symbol {template_ref}")

            # Set reference
            reference = component_def.get("reference", "R?")
            new_symbol.property.Reference.value = reference
            logger.debug(f"Set reference to {reference}")

            # Set value
            if "value" in component_def:
                new_symbol.property.Value.value = component_def["value"]
                logger.debug(f"Set value to {component_def['value']}")

            # Set footprint
            if "footprint" in component_def:
                new_symbol.property.Footprint.value = component_def["footprint"]
                logger.debug(f"Set footprint to {component_def['footprint']}")

            # Set datasheet
            if "datasheet" in component_def:
                new_symbol.property.Datasheet.value = component_def["datasheet"]

            # Set position
            x = component_def.get("x", 0)
            y = component_def.get("y", 0)
            rotation = component_def.get("rotation", 0)
            new_symbol.at.value = [x, y, rotation]
            logger.debug(f"Set position to ({x}, {y}, {rotation})")

            # Set BOM and board flags
            new_symbol.in_bom.value = component_def.get("in_bom", True)
            new_symbol.on_board.value = component_def.get("on_board", True)
            new_symbol.dnp.value = component_def.get("dnp", False)

            # Generate new UUID
            new_symbol.uuid.value = str(uuid.uuid4())

            # NOTE: clone() already inserts the raw element into the schematic tree.
            # Calling schematic.symbol.append() again causes NamedCollection to detect
            # the reference as "taken" and rename it to "R1_" (trailing underscore).
            logger.info(f"Successfully added component {reference} to schematic")

            return new_symbol
        except Exception as e:
            logger.error(f"Error adding component: {e}", exc_info=True)
            raise
