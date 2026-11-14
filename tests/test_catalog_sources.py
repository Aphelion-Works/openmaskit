"""Catalog list and per-catalog token loading."""

import json

import pytest

from openmaskit.catalog_sources import (
    CatalogSource,
    load_catalog_sources,
    request_headers,
    save_catalogs,
    validate_catalog_url,
)


def test_saved_catalogs_live_beside_server_state(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENMASKIT_MARKETPLACE_CATALOGS", raising=False)
    monkeypatch.delenv("OPENMASKIT_MARKETPLACE_TOKENS_DIR", raising=False)
    saved = save_catalogs(
        [
            {
                "id": "community",
                "name": "Community",
                "url": "https://aphelion-works.github.io/openmaskit-catalog",
            },
            {
                "name": "Personal",
                "url": "https://raw.githubusercontent.com/owner/openmaskit-catalog/main",
                "token": "stored-token",
            },
        ],
        state_dir=tmp_path,
    )
    assert [source.id for source in saved] == ["community", "personal"]
    assert saved[1].token == "stored-token"
    listing = (tmp_path / "catalogs.json").read_text()
    assert "stored-token" not in listing
    assert (tmp_path / "catalog-tokens" / "personal").read_text() == "stored-token\n"
    reloaded, problems = load_catalog_sources(state_dir=tmp_path)
    assert problems == []
    assert reloaded[1].token == "stored-token"


def test_default_catalog_is_the_community_pages_site(monkeypatch):
    monkeypatch.delenv("OPENMASKIT_MARKETPLACE_CATALOGS", raising=False)
    monkeypatch.delenv("OPENMASKIT_MARKETPLACE_API_URL", raising=False)
    sources, problems = load_catalog_sources()
    assert problems == []
    assert sources[0].id == "community"
    assert sources[0].url == "https://aphelion-works.github.io/openmaskit-catalog"
    assert sources[0].token is None


def test_catalog_file_replaces_the_default_and_reads_one_token(tmp_path, monkeypatch):
    listing = tmp_path / "catalogs.json"
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "personal").write_text("stored-token\n")
    listing.write_text(
        json.dumps(
            {
                "catalogs": [
                    {
                        "id": "community",
                        "name": "Community",
                        "url": "https://aphelion-works.github.io/openmaskit-catalog",
                    },
                    {
                        "id": "personal",
                        "name": "Personal",
                        "url": "https://raw.githubusercontent.com/owner/openmaskit-catalog/main",
                    },
                ]
            }
        )
    )
    monkeypatch.setenv("OPENMASKIT_MARKETPLACE_CATALOGS", str(listing))
    monkeypatch.setenv("OPENMASKIT_MARKETPLACE_TOKENS_DIR", str(tokens))
    sources, problems = load_catalog_sources()
    assert problems == []
    assert [source.id for source in sources] == ["community", "personal"]
    assert sources[0].token is None
    assert sources[1].token == "stored-token"
    assert "stored-token" not in repr(sources[1])


def test_catalog_file_rejects_an_embedded_token(tmp_path, monkeypatch):
    listing = tmp_path / "catalogs.json"
    listing.write_text(
        json.dumps(
            {
                "catalogs": [
                    {
                        "id": "personal",
                        "name": "Personal",
                        "url": "https://raw.githubusercontent.com/owner/openmaskit-catalog/main",
                        "token": "nope",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("OPENMASKIT_MARKETPLACE_CATALOGS", str(listing))
    sources, problems = load_catalog_sources()
    assert sources[0].id == "community"
    assert problems[0]["status"] == "unavailable"
    assert "token" in problems[0]["message"].lower()


def test_token_is_not_sent_to_another_repository():
    source = CatalogSource(
        "personal",
        "Personal",
        "https://raw.githubusercontent.com/owner/openmaskit-catalog/main",
        token="stored-token",
    )
    own = request_headers(
        source,
        "https://raw.githubusercontent.com/owner/openmaskit-catalog/main/api/marketplace/catalog",
    )
    other = request_headers(
        source,
        "https://raw.githubusercontent.com/other/openmaskit-catalog/main/api/marketplace/catalog",
    )
    assert own["Authorization"] == "Bearer stored-token"
    assert other == {}


def test_symlink_token_is_not_read(tmp_path, monkeypatch):
    listing = tmp_path / "catalogs.json"
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("stolen")
    (tokens / "personal").symlink_to(outside)
    listing.write_text(
        json.dumps(
            {
                "catalogs": [
                    {
                        "id": "personal",
                        "name": "Personal",
                        "url": "https://private.example",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv("OPENMASKIT_MARKETPLACE_CATALOGS", str(listing))
    monkeypatch.setenv("OPENMASKIT_MARKETPLACE_TOKENS_DIR", str(tokens))
    sources, problems = load_catalog_sources()
    assert sources[0].token is None
    assert problems[0]["id"] == "personal"


@pytest.mark.parametrize(
    "value",
    [
        "http://catalog.example",
        "https://user:secret@catalog.example",
        "https://catalog.example/path?q=1",
        "https://catalog.example/../secret",
    ],
)
def test_catalog_address_rejects_unsafe_urls(value):
    with pytest.raises(ValueError):
        validate_catalog_url(value)
