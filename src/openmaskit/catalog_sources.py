"""Catalog sources configured on this machine.

A source is a catalog address plus an optional read token. Tokens stay in
files next to the catalog list and are sent only to that source's host.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

DEFAULT_CATALOG_URL = "https://aphelion-works.github.io/openmaskit-catalog"
_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62})$")
_TOKEN_KEYS = {"token", "authorization", "password", "secret"}


@dataclass(frozen=True)
class CatalogSource:
    """One catalog the marketplace can read."""

    id: str
    name: str
    url: str
    token: str | None = field(default=None, repr=False)


def validate_catalog_url(value: str) -> str:
    """Return a canonical https origin, or raise ValueError."""
    text = str(value or "").strip()
    parsed = urlsplit(text)
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or ".." in path.split("/")
    ):
        raise ValueError("A catalog address must be an https URL")
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"https://{host}{path}"


def request_headers(source: CatalogSource, url: str) -> dict[str, str]:
    """Bearer token for this source, and only when the request host matches."""
    if not source.token:
        return {}
    requested = urlsplit(url)
    allowed = urlsplit(source.url)
    requested_path = requested.path.rstrip("/")
    allowed_path = allowed.path.rstrip("/")
    path_matches = not allowed_path or requested_path == allowed_path or requested_path.startswith(
        allowed_path + "/"
    )
    if (
        requested.scheme != allowed.scheme
        or requested.hostname != allowed.hostname
        or requested.port != allowed.port
        or not path_matches
    ):
        return {}
    return {"Authorization": f"Bearer {source.token}"}


def catalog_paths(
    environ: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Return the catalog file and token directory, or none when fixed."""
    env = os.environ if environ is None else environ
    configured = env.get("OPENMASKIT_MARKETPLACE_CATALOGS", "").strip()
    if configured:
        path = Path(configured)
        tokens = env.get("OPENMASKIT_MARKETPLACE_TOKENS_DIR", "").strip()
        return path, Path(tokens) if tokens else path.parent / "catalog-tokens"
    if state_dir:
        base = Path(state_dir).expanduser()
        return base / "catalogs.json", base / "catalog-tokens"
    return None, None


def load_catalog_sources(
    environ: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> tuple[list[CatalogSource], list[dict[str, str]]]:
    """Load the configured catalogs.

    The catalog file replaces the single default address. A missing file uses
    ``OPENMASKIT_MARKETPLACE_API_URL`` or the community catalog. Problems are
    returned beside the sources so a rejected token is not an empty list.
    """
    env = os.environ if environ is None else environ
    path, _tokens = catalog_paths(env, state_dir)
    if path is None or not path.is_file():
        return [_default_source(env)], []
    try:
        sources = _read_catalog_file(path)
        tokens, problems = _read_tokens(sources, env, state_dir)
    except ValueError as exc:
        return [_default_source(env)], [
            {"id": "catalogs", "name": "Catalogs", "status": "unavailable", "message": str(exc)}
        ]
    loaded = [
        CatalogSource(source.id, source.name, source.url, tokens.get(source.id))
        for source in sources
    ]
    return loaded, problems


def _default_source(env: Mapping[str, str]) -> CatalogSource:
    raw = env.get("OPENMASKIT_MARKETPLACE_API_URL", DEFAULT_CATALOG_URL)
    try:
        url = validate_catalog_url(raw)
    except ValueError:
        url = _loopback_http_url(raw) or DEFAULT_CATALOG_URL
    source_id = "community" if url == DEFAULT_CATALOG_URL else "default"
    name = "Community" if source_id == "community" else "Catalog"
    return CatalogSource(source_id, name, url)


def _loopback_http_url(value: str) -> str | None:
    """Allow the local acceptance catalog. Saved catalog addresses stay https."""
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "http"
        or host not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or ".." in parsed.path.split("/")
    ):
        return None
    path = parsed.path.rstrip("/")
    if host == "::1":
        netloc = f"[::1]:{parsed.port}" if parsed.port else "[::1]"
    else:
        netloc = host
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
    return f"http://{netloc}{path}"


def _read_catalog_file(path: Path) -> list[CatalogSource]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("The catalog list must be a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("The catalog list could not be read") from exc
    entries = payload.get("catalogs") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("The catalog list needs at least one catalog")
    sources: list[CatalogSource] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each catalog needs an id, name, and address")
        if _TOKEN_KEYS.intersection(key.lower() for key in entry):
            raise ValueError("The catalog list cannot contain tokens")
        source_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not _ID.fullmatch(source_id) or not name or len(name) > 80:
            raise ValueError("Each catalog needs an id, name, and address")
        if source_id in seen:
            raise ValueError("Catalog ids must be unique")
        seen.add(source_id)
        url = validate_catalog_url(str(entry.get("url") or ""))
        sources.append(CatalogSource(source_id, name, url))
    return sources


def _read_tokens(
    sources: list[CatalogSource],
    env: Mapping[str, str],
    state_dir: str | Path | None = None,
) -> tuple[dict[str, str], list[dict[str, str]]]:
    _path, directory = catalog_paths(env, state_dir)
    if directory is None or not directory.exists():
        return {}, []
    if directory.is_symlink() or not directory.is_dir():
        return {}, [
            {
                "id": "tokens",
                "name": "Catalog tokens",
                "status": "unavailable",
                "message": "The catalog token directory could not be read",
            }
        ]
    tokens: dict[str, str] = {}
    problems: list[dict[str, str]] = []
    for source in sources:
        path = directory / source.id
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            problems.append(_token_problem(source, "The catalog token could not be read"))
            continue
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            problems.append(_token_problem(source, "The catalog token could not be read"))
            continue
        if not token or any(char in token for char in "\r\n\x00"):
            problems.append(_token_problem(source, "The catalog token could not be read"))
            continue
        tokens[source.id] = token
    return tokens, problems


def _token_problem(source: CatalogSource, message: str) -> dict[str, str]:
    return {"id": source.id, "name": source.name, "status": "unavailable", "message": message}


def describe_catalogs(
    sources: list[CatalogSource], *, editable: bool
) -> dict[str, object]:
    """Public catalog settings. Token values are not included."""
    return {
        "editable": editable,
        "catalogs": [
            {
                "id": source.id,
                "name": source.name,
                "url": source.url,
                "token_saved": bool(source.token),
            }
            for source in sources
        ],
    }


def save_catalogs(
    entries: list[dict],
    *,
    environ: Mapping[str, str] | None = None,
    state_dir: str | Path | None = None,
) -> list[CatalogSource]:
    """Replace the catalog list stored beside this server's state."""
    path, token_dir = catalog_paths(environ, state_dir)
    if path is None or token_dir is None:
        raise ValueError("Catalogs are not editable")
    if path.is_symlink() or token_dir.is_symlink():
        raise ValueError("Managed paths must not be symbolic links")
    current, _problems = load_catalog_sources(environ, state_dir)
    saved_ids = {source.id for source in current if source.token}
    cleaned: list[CatalogSource] = []
    used: set[str] = set()
    replacements: dict[str, str] = {}
    cleared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each catalog needs an id, name, and address")
        source_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not source_id:
            source_id = _new_id(name, used)
        if not _ID.fullmatch(source_id) or source_id in used or not name or len(name) > 80:
            raise ValueError("Each catalog needs an id, name, and address")
        used.add(source_id)
        url = validate_catalog_url(str(entry.get("url") or ""))
        token = entry.get("token")
        if isinstance(token, str) and token.strip():
            if any(char in token for char in "\r\n\x00"):
                raise ValueError("The catalog token could not be saved")
            replacements[source_id] = token.strip()
        elif entry.get("clear_token"):
            cleared.add(source_id)
        cleaned.append(CatalogSource(source_id, name, url))
    if not cleaned:
        raise ValueError("At least one catalog is required")
    path.parent.mkdir(mode=0o700, exist_ok=True)
    token_dir.mkdir(mode=0o700, exist_ok=True)
    _write_json(
        path,
        {"catalogs": [{"id": source.id, "name": source.name, "url": source.url} for source in cleaned]},
    )
    for source_id, token in replacements.items():
        target = token_dir / source_id
        if target.is_symlink():
            raise ValueError("Managed paths must not be symbolic links")
        _write_text(target, token + "\n", 0o600)
    for source_id in cleared:
        target = token_dir / source_id
        if target.is_symlink():
            raise ValueError("Managed paths must not be symbolic links")
        if target.is_file():
            target.unlink()
    removed = (saved_ids | {source.id for source in current}) - {source.id for source in cleaned}
    for source_id in removed:
        target = token_dir / source_id
        if target.is_file() and not target.is_symlink():
            target.unlink()
    loaded, problems = load_catalog_sources(environ, state_dir)
    if problems:
        raise ValueError(problems[0]["message"])
    return loaded


def _new_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "catalog"
    candidate = base
    number = 2
    while candidate in used or not _ID.fullmatch(candidate):
        suffix = f"-{number}"
        candidate = f"{base[: 63 - len(suffix)]}{suffix}"
        number += 1
    return candidate


def _write_json(path: Path, payload: dict) -> None:
    _write_text(path, json.dumps(payload, indent=2) + "\n", 0o644)


def _write_text(path: Path, text: str, mode: int) -> None:
    if path.is_symlink():
        raise ValueError("Managed paths must not be symbolic links")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(mode)
    temporary.replace(path)
    path.chmod(mode)
