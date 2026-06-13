import glob
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class LibraryManager:
    """Manage symbol libraries"""

    @staticmethod
    def list_available_libraries(search_paths: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """List all available symbol libraries"""
        if search_paths is None:
            # Default library paths based on common KiCAD installations
            # This would need to be configured for the specific environment
            search_paths = [
                "C:/Program Files/KiCad/*/share/kicad/symbols/*.kicad_sym",  # Windows path pattern
                "/usr/share/kicad/symbols/*.kicad_sym",  # Linux path pattern
                "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols/*.kicad_sym",  # macOS path pattern
                os.path.expanduser(
                    "~/Documents/KiCad/*/symbols/*.kicad_sym"
                ),  # User libraries pattern
            ]

        libraries = []
        for path_pattern in search_paths:
            try:
                # Use glob to find all matching files
                matching_libs = glob.glob(path_pattern, recursive=True)
                libraries.extend(matching_libs)
            except (OSError, ValueError) as e:
                logger.exception(f"Error searching for libraries at {path_pattern}: {e}")

        # Extract library names from paths
        library_names = [os.path.splitext(os.path.basename(lib))[0] for lib in libraries]
        logger.info(
            f"Found {len(library_names)} libraries: {', '.join(library_names[:10])}{'...' if len(library_names) > 10 else ''}"
        )

        # Return both full paths and library names
        return {"paths": libraries, "names": library_names}


if __name__ == "__main__":
    # Example Usage (for testing)
    # List available libraries
    libraries = LibraryManager.list_available_libraries()
