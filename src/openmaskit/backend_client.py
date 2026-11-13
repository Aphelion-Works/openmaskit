"""HTTP client for OpenMaskit backend services."""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BackendClient:
    """HTTP client for catalog reads. OAuth stays on this machine."""

    def __init__(
        self,
        installation_id: str,
        openmaskit_version: str,
        marketplace_url: str | None = None,
        timeout: float = 10.0,
    ):
        self.marketplace_url = marketplace_url or os.getenv(
            "OPENMASKIT_MARKETPLACE_API_URL", "https://api.maskitmcp.com"
        )
        self.client = httpx.AsyncClient(timeout=timeout)
        # OPENMASKIT_DISABLE_MARKETPLACE=1 opts out of catalog browse,
        # server detail, and version check.
        self.enabled = os.getenv("OPENMASKIT_DISABLE_MARKETPLACE", "").strip() not in (
            "1", "true", "True", "yes",
        )
        self.installation_id = installation_id
        self.openmaskit_version = openmaskit_version

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    # Marketplace API

    async def get_catalog(
        self, page: int = 1, size: int = 12, query: str | None = None
    ) -> dict[str, Any]:
        """Fetch marketplace catalog from backend.

        Args:
            page: Page number (1-indexed)
            size: Items per page
            query: Optional text search query

        Returns:
            Dict with 'data' (list of servers) and 'meta' (pagination info).
            Returns empty result on error: {"data": [], "meta": {"total": 0, "page": 1, "size": size, "total_pages": 0}}
        """
        if not self.marketplace_url or not self.enabled:
            return {"data": [], "meta": {"total": 0, "page": 1, "size": size, "total_pages": 0}}

        try:
            params = {"page": page, "size": size}
            if query:
                params["q"] = query

            resp = await self.client.get(
                f"{self.marketplace_url}/api/marketplace/catalog",
                params=params,
            )
            resp.raise_for_status()
            response_data = resp.json()

            # Backend returns object with data and meta
            if isinstance(response_data, dict) and "data" in response_data and "meta" in response_data:
                return response_data
            else:
                logger.warning(f"Unexpected catalog response format: {type(response_data)}")
                return {"data": [], "meta": {"total": 0, "page": 1, "size": size, "total_pages": 0}}

        except Exception as e:
            logger.warning(f"Failed to fetch backend catalog: {e}")
            return {"data": [], "meta": {"total": 0, "page": 1, "size": size, "total_pages": 0}}

    async def check_version(self) -> dict[str, Any] | None:
        """Ask the marketplace whether this client version is supported.

        Returns the parsed response body, or None on any failure (fail-open).
        The request does not send an installation id or an OpenMaskit version.
        """
        if not self.marketplace_url or not self.enabled:
            return None
        try:
            resp = await self.client.get(
                f"{self.marketplace_url}/api/version_check",
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"version_check failed: {e}")
            return None

    async def get_server_info(self, server_id: str) -> dict[str, Any] | None:
        """Get server details by UUID.

        Returns None on error.
        """
        if not self.marketplace_url or not self.enabled:
            return None

        try:
            resp = await self.client.get(
                f"{self.marketplace_url}/api/marketplace/servers/{server_id}",
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Failed to get server info for {server_id}: {e}")
            return None
