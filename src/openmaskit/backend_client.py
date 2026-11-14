"""HTTP client for catalog reads. OAuth stays on this machine."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

from openmaskit.catalog_sources import (
    CatalogSource,
    catalog_paths,
    describe_catalogs,
    load_catalog_sources,
    request_headers,
    save_catalogs,
    validate_catalog_url,
)

logger = logging.getLogger(__name__)

_EMPTY_META = {"total": 0, "page": 1, "size": 12, "total_pages": 0}


class BackendClient:
    """Read one or more catalogs. Tokens are sent only to their own host."""

    def __init__(
        self,
        installation_id: str,
        openmaskit_version: str,
        marketplace_url: str | None = None,
        timeout: float = 10.0,
        state_dir: str | None = None,
    ):
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        # OPENMASKIT_DISABLE_MARKETPLACE=1 opts out of catalog browse,
        # server detail, and version check.
        self.enabled = os.getenv("OPENMASKIT_DISABLE_MARKETPLACE", "").strip() not in (
            "1", "true", "True", "yes",
        )
        self.installation_id = installation_id
        self.openmaskit_version = openmaskit_version
        self.state_dir = state_dir
        self._fixed_source = bool(marketplace_url)
        self.reload_sources(marketplace_url)

    def reload_sources(self, marketplace_url: str | None = None) -> None:
        """Reread the catalog list stored beside this server's state."""
        if marketplace_url or self._fixed_source:
            url = validate_catalog_url(marketplace_url or self.marketplace_url)
            self.sources = [CatalogSource("default", "Catalog", url)]
            self.catalog_problems = []
            self.editable = False
        else:
            self.sources, self.catalog_problems = load_catalog_sources(state_dir=self.state_dir)
            self.editable = catalog_paths(state_dir=self.state_dir)[0] is not None
        self.marketplace_url = self.sources[0].url if self.sources else ""

    def catalog_settings(self) -> dict[str, object]:
        return describe_catalogs(self.sources, editable=self.editable)

    def replace_catalogs(self, entries: list[dict]) -> dict[str, object]:
        save_catalogs(entries, state_dir=self.state_dir)
        self.reload_sources()
        return self.catalog_settings()

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def get_catalog(
        self, page: int = 1, size: int = 12, query: str | None = None
    ) -> dict[str, Any]:
        """Fetch and merge every configured catalog.

        A source that rejects its token is reported in ``catalogs`` and does
        not erase entries from the other sources.
        """
        if not self.enabled or not self.sources:
            return {"data": [], "meta": {**_EMPTY_META, "size": size}, "catalogs": []}

        merged: list[dict[str, Any]] = []
        catalogs: list[dict[str, Any]] = []
        single = len(self.sources) == 1
        upstream_meta = None
        for source in self.sources:
            if any(problem["id"] == source.id for problem in self.catalog_problems):
                continue
            payload, status = await self._fetch_catalog(source, page, size, query, single)
            catalogs.append(status)
            if payload is None:
                continue
            upstream_meta = payload["meta"]
            for entry in payload["data"]:
                if not isinstance(entry, dict):
                    continue
                stamped = dict(entry)
                stamped["catalog_id"] = source.id
                stamped["catalog_name"] = source.name
                merged.append(stamped)

        catalogs.extend(self.catalog_problems)
        if not single:
            if query:
                needle = query.lower()
                merged = [entry for entry in merged if _matches(entry, needle)]
            total = len(merged)
            start = max(page - 1, 0) * size
            merged = merged[start : start + size]
            pages = max(1, (total + size - 1) // size) if total else 0
            meta = {"total": total, "page": page, "size": size, "total_pages": pages}
        else:
            meta = upstream_meta or {**_EMPTY_META, "page": page, "size": size}
        return {"data": merged, "meta": meta, "catalogs": catalogs}

    async def check_version(self) -> dict[str, Any] | None:
        """Ask the first catalog whether this client version is supported.

        Other catalogs do not decide update requirements. The request does not
        send an installation id or an OpenMaskit version. Returns None on failure.
        """
        if not self.enabled or not self.sources:
            return None
        source = self.sources[0]
        url = f"{source.url}/api/version_check"
        try:
            response = await self.client.get(url, headers=request_headers(source, url))
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.warning("version_check failed for %s: %s", source.id, exc)
            return None

    async def get_server_info(
        self, server_id: str, catalog_id: str | None = None
    ) -> dict[str, Any] | None:
        """Get server details from the named catalog, or the first that has it."""
        if not self.enabled or not self.sources:
            return None
        sources = self.sources
        if catalog_id:
            sources = [source for source in self.sources if source.id == catalog_id]
        for source in sources:
            url = f"{source.url}/api/marketplace/servers/{server_id}"
            try:
                response = await self.client.get(url, headers=request_headers(source, url))
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                logger.warning("Failed to get server info for %s from %s: %s", server_id, source.id, exc)
                continue
            if isinstance(payload, dict):
                payload = dict(payload)
                payload["catalog_id"] = source.id
                payload["catalog_name"] = source.name
                return payload
        return None

    async def _fetch_catalog(
        self,
        source: CatalogSource,
        page: int,
        size: int,
        query: str | None,
        single: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        request_page = page if single else 1
        request_size = size if single else max(size, 100)
        params: dict[str, Any] = {"page": request_page, "size": request_size}
        if query:
            params["q"] = query
        url = f"{source.url}/api/marketplace/catalog"
        try:
            response = await self.client.get(
                url, params=params, headers=request_headers(source, url)
            )
        except Exception as exc:
            logger.warning("Failed to fetch catalog %s: %s", source.id, exc)
            return None, _status(source, "unavailable", "The catalog could not be reached")
        if response.status_code in (401, 403):
            logger.warning("Catalog %s rejected the saved token", source.id)
            return None, _status(source, "unauthorized", "The catalog rejected the saved token")
        try:
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("Failed to fetch catalog %s: %s", source.id, exc)
            return None, _status(source, "unavailable", "The catalog could not be reached")
        if not isinstance(payload, dict) or "data" not in payload or "meta" not in payload:
            logger.warning("Unexpected catalog response format from %s", source.id)
            return None, _status(source, "unavailable", "The catalog could not be reached")
        data = payload["data"] if isinstance(payload["data"], list) else []
        meta = payload["meta"] if isinstance(payload["meta"], dict) else {}
        state = "partial" if int(meta.get("total_pages") or 1) > 1 and not single else "ok"
        message = "This catalog has more entries than were merged" if state == "partial" else ""
        return {"data": data, "meta": meta}, _status(source, state, message, len(data))


def _status(
    source: CatalogSource, status: str, message: str, count: int = 0
) -> dict[str, Any]:
    host = urlsplit(source.url).hostname or ""
    return {
        "id": source.id,
        "name": source.name,
        "host": host,
        "status": status,
        "message": message,
        "count": count,
    }


def _matches(entry: dict[str, Any], needle: str) -> bool:
    tags = entry.get("tags") or []
    parts = [
        str(entry.get("name") or ""),
        str(entry.get("handle") or ""),
        str(entry.get("description") or ""),
        " ".join(str(tag) for tag in tags),
    ]
    return needle in " ".join(parts).lower()
