"""
JLCSearch API client (public, no authentication required)

Alternative to official JLCPCB API using the community-maintained
jlcsearch service at https://jlcsearch.tscircuit.com/
"""

import logging
import time
from typing import Callable, Dict, List, Optional

import requests

logger = logging.getLogger("kicad_interface")


class JLCSearchClient:
    """
    Client for JLCSearch public API (tscircuit)

    Provides access to JLCPCB parts database without authentication
    via the community-maintained jlcsearch service.
    """

    BASE_URL = "https://jlcsearch.tscircuit.com"

    def __init__(self) -> None:
        """Initialize JLCSearch API client"""

    def search_components(
        self, category: str = "components", limit: int = 100, offset: int = 0, **filters: Dict
    ) -> List[Dict]:
        """
        Search components in JLCSearch database

        Args:
            category: Component category (e.g., "resistors", "capacitors", "components")
            limit: Maximum number of results
            offset: Offset for pagination
            **filters: Additional filters (e.g., package="0603", resistance=1000)

        Returns:
            List of component dicts
        """
        url = f"{self.BASE_URL}/{category}/list.json"

        params = {"limit": limit, "offset": offset, **filters}

        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            # The response has the category name as key
            # e.g., {"resistors": [...]} or {"components": [...]}
            for key, value in data.items():
                if isinstance(value, list):
                    return value

            return []

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to search JLCSearch: {e}")
            raise Exception(f"JLCSearch API request failed: {e}")

    def download_all_components(
        self, callback: Optional[Callable[[int, str], None]] = None, batch_size: int = 100
    ) -> List[Dict]:
        """
        Download all components from jlcsearch database

        Note: tscircuit API has a hard-coded 100 result limit per request.
        Full catalog download requires ~25,000 paginated requests (~40-60 minutes).

        Args:
            callback: Optional progress callback function(parts_count, status_msg)
            batch_size: Number of parts per batch (max 100 due to API limit)

        Returns:
            List of all parts
        """
        all_parts = []
        offset = 0

        logger.info("Starting full jlcsearch parts database download...")

        while True:
            try:
                batch = self.search_components("components", limit=batch_size, offset=offset)

                # Stop if no results returned (end of catalog)
                if not batch or len(batch) == 0:
                    break

                all_parts.extend(batch)
                offset += len(batch)

                if callback:
                    callback(len(all_parts), f"Downloaded {len(all_parts)} parts...")
                else:
                    logger.info(f"Downloaded {len(all_parts)} parts so far...")

                # Continue pagination - API returns exactly 100 results per page until exhausted
                # Only stop when we get 0 results (handled above)

                # Rate limiting - be nice to the API
                time.sleep(0.1)

            except Exception as e:
                logger.error(f"Error downloading parts at offset {offset}: {e}")
                if len(all_parts) > 0:
                    logger.warning(f"Partial download available: {len(all_parts)} parts")
                    return all_parts
                else:
                    raise

        logger.info(f"Download complete: {len(all_parts)} parts retrieved")
        return all_parts
