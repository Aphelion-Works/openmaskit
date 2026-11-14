"""Tests for backend client."""

import pytest
import pytest_asyncio
import httpx
import respx

from openmaskit.backend_client import BackendClient


def assert_catalog_request_has_no_identity(headers) -> None:
    names = {name.lower() for name in headers}
    assert "x-openmaskit-installation-id" not in names
    assert not headers.get("user-agent", "").startswith("OpenMaskit/")


@pytest_asyncio.fixture
async def client():
    """Create backend client instance."""
    c = BackendClient(
        installation_id="test-install-123",
        openmaskit_version="0.1.0",
        marketplace_url="https://test-api.example.com",
        timeout=5.0,
    )
    yield c
    await c.close()


class TestBackendClientInit:
    """Test client initialization and configuration."""

    @pytest.mark.anyio
    async def test_init_with_explicit_urls(self):
        """Initialize client with explicit URLs."""
        client = BackendClient(
            installation_id="test-123",
            openmaskit_version="1.0.0",
            marketplace_url="https://custom-api.com",
        )
        assert client.marketplace_url == "https://custom-api.com"
        assert client.installation_id == "test-123"
        assert client.openmaskit_version == "1.0.0"
        assert client.enabled is True
        await client.close()

    @pytest.mark.anyio
    async def test_init_with_env_vars(self, monkeypatch):
        """Initialize client with environment variables."""
        monkeypatch.setenv("OPENMASKIT_MARKETPLACE_API_URL", "https://env-api.com")

        client = BackendClient(
            installation_id="test-456",
            openmaskit_version="2.0.0",
        )
        assert client.marketplace_url == "https://env-api.com"
        await client.close()

    @pytest.mark.anyio
    async def test_init_with_defaults(self, monkeypatch):
        """Initialize client with default URLs when no config provided."""
        monkeypatch.delenv("OPENMASKIT_MARKETPLACE_API_URL", raising=False)

        client = BackendClient(
            installation_id="test-789",
            openmaskit_version="3.0.0",
        )
        assert (
            client.marketplace_url
            == "https://aphelion-works.github.io/openmaskit-catalog"
        )
        await client.close()

    @pytest.mark.anyio
    async def test_identity_is_not_prepared_for_catalog_requests(self):
        """Catalog calls do not carry an installation id or OpenMaskit version."""
        client = BackendClient(
            installation_id="test-abc",
            openmaskit_version="4.5.6",
        )
        assert not hasattr(client, "required_headers")
        await client.close()

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", ["1", "true", "True", "yes"])
    async def test_disable_marketplace_env_var(self, monkeypatch, value):
        """OPENMASKIT_DISABLE_MARKETPLACE truthy values disable backend calls."""
        monkeypatch.setenv("OPENMASKIT_DISABLE_MARKETPLACE", value)
        client = BackendClient(installation_id="x", openmaskit_version="0.6.0")
        assert client.enabled is False
        await client.close()

    @pytest.mark.anyio
    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
    async def test_marketplace_enabled_by_default(self, monkeypatch, value):
        """Falsy/empty values keep marketplace enabled."""
        if value == "":
            monkeypatch.delenv("OPENMASKIT_DISABLE_MARKETPLACE", raising=False)
        else:
            monkeypatch.setenv("OPENMASKIT_DISABLE_MARKETPLACE", value)
        client = BackendClient(installation_id="x", openmaskit_version="0.6.0")
        assert client.enabled is True
        await client.close()

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_short_circuits_when_disabled(self, monkeypatch):
        """get_catalog returns empty result without making a request when disabled."""
        monkeypatch.setenv("OPENMASKIT_DISABLE_MARKETPLACE", "1")
        client = BackendClient(
            installation_id="x",
            openmaskit_version="0.6.0",
            marketplace_url="https://test-api.example.com",
        )
        # Intentionally do NOT register any respx route — if the client tries
        # to send a request, respx will raise.
        result = await client.get_catalog()
        assert result["data"] == []
        assert result["catalogs"] == []
        await client.close()

    @pytest.mark.anyio
    @respx.mock
    async def test_get_server_info_short_circuits_when_disabled(self, monkeypatch):
        """get_server_info returns None without making a request when disabled."""
        monkeypatch.setenv("OPENMASKIT_DISABLE_MARKETPLACE", "1")
        client = BackendClient(
            installation_id="x",
            openmaskit_version="0.6.0",
            marketplace_url="https://test-api.example.com",
        )
        result = await client.get_server_info("any-id")
        assert result is None
        await client.close()

    @pytest.mark.anyio
    @respx.mock
    async def test_check_version_short_circuits_when_disabled(self, monkeypatch):
        """check_version returns None without making a request when disabled."""
        monkeypatch.setenv("OPENMASKIT_DISABLE_MARKETPLACE", "1")
        client = BackendClient(
            installation_id="x",
            openmaskit_version="0.6.0",
            marketplace_url="https://test-api.example.com",
        )
        result = await client.check_version()
        assert result is None
        await client.close()


class TestMarketplaceCatalog:
    """Test marketplace catalog fetching."""

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_success(self, client):
        """Fetch catalog successfully."""
        catalog_data = {
            "data": [
                {
                    "id": "server-1",
                    "name": "Test Server 1",
                    "description": "A test server",
                    "icon_url": "https://example.com/icon1.png",
                },
                {
                    "id": "server-2",
                    "name": "Test Server 2",
                    "description": "Another test server",
                    "icon_url": "https://example.com/icon2.png",
                },
            ],
            "meta": {
                "total": 2,
                "page": 1,
                "size": 12,
                "total_pages": 1,
            },
        }

        respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(200, json=catalog_data)
        )

        result = await client.get_catalog()
        assert len(result["data"]) == 2
        assert result["data"][0]["catalog_id"] == "default"
        assert result["meta"]["total"] == 2
        assert result["catalogs"][0]["status"] == "ok"

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_with_pagination(self, client):
        """Fetch catalog with pagination parameters."""
        route = respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"id": "s1"}],
                    "meta": {"total": 50, "page": 3, "size": 20, "total_pages": 3},
                },
            )
        )

        result = await client.get_catalog(page=3, size=20)
        assert result["meta"]["page"] == 3
        assert result["meta"]["size"] == 20

        # Verify request parameters
        request = route.calls.last.request
        assert request.url.params["page"] == "3"
        assert request.url.params["size"] == "20"

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_with_search_query(self, client):
        """Fetch catalog with search query."""
        route = respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"id": "github", "name": "GitHub"}],
                    "meta": {"total": 1, "page": 1, "size": 12, "total_pages": 1},
                },
            )
        )

        result = await client.get_catalog(query="github")
        assert len(result["data"]) == 1
        assert result["data"][0]["name"] == "GitHub"

        # Verify search param
        request = route.calls.last.request
        assert request.url.params["q"] == "github"

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_omits_installation_identity(self, client):
        """Catalog browse does not send an installation id or OpenMaskit version."""
        route = respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(
                200,
                json={"data": [], "meta": {"total": 0, "page": 1, "size": 12, "total_pages": 0}},
            )
        )

        await client.get_catalog()

        assert_catalog_request_has_no_identity(route.calls.last.request.headers)

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_http_error(self, client):
        """Handle HTTP errors gracefully."""
        respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        result = await client.get_catalog()
        assert result["data"] == []
        assert result["catalogs"][0]["status"] == "unavailable"

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_network_error(self, client):
        """Handle network errors gracefully."""
        respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            side_effect=httpx.ConnectError("Connection failed")
        )

        result = await client.get_catalog()
        assert result["data"] == []
        assert result["catalogs"][0]["status"] == "unavailable"

    @pytest.mark.anyio
    @respx.mock
    async def test_get_catalog_invalid_response_format(self, client):
        """Handle invalid response format."""
        respx.get("https://test-api.example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(200, json=["not", "expected", "format"])
        )

        result = await client.get_catalog()
        assert result["data"] == []
        assert result["catalogs"][0]["status"] == "unavailable"

    @pytest.mark.anyio
    async def test_get_catalog_no_marketplace_url(self):
        """Return empty when marketplace URL is None."""
        client = BackendClient(
            installation_id="test",
            openmaskit_version="1.0.0",
            marketplace_url=None,
        )

        result = await client.get_catalog()
        assert result["data"] == []
        await client.close()


class TestServerInfo:
    """Test fetching server details."""

    @pytest.mark.anyio
    @respx.mock
    async def test_get_server_info_success(self, client):
        """Fetch server info successfully."""
        server_data = {
            "id": "test-server-uuid",
            "name": "Test Server",
            "description": "A test MCP server",
            "icon_url": "https://example.com/icon.png",
            "config": {"transport": "http", "url": "https://mcp.example.com"},
        }

        route = respx.get(
            "https://test-api.example.com/api/marketplace/servers/test-server-uuid"
        ).mock(return_value=httpx.Response(200, json=server_data))

        result = await client.get_server_info("test-server-uuid")
        assert result["name"] == "Test Server"
        assert result["catalog_id"] == "default"
        assert_catalog_request_has_no_identity(route.calls.last.request.headers)

    @pytest.mark.anyio
    @respx.mock
    async def test_get_server_info_not_found(self, client):
        """Handle 404 when server not found."""
        respx.get(
            "https://test-api.example.com/api/marketplace/servers/nonexistent"
        ).mock(return_value=httpx.Response(404, text="Not Found"))

        result = await client.get_server_info("nonexistent")
        assert result is None

    @pytest.mark.anyio
    @respx.mock
    async def test_get_server_info_network_error(self, client):
        """Handle network errors."""
        respx.get(
            "https://test-api.example.com/api/marketplace/servers/test-id"
        ).mock(side_effect=httpx.TimeoutException("Request timeout"))

        result = await client.get_server_info("test-id")
        assert result is None

    @pytest.mark.anyio
    async def test_get_server_info_no_marketplace_url(self):
        """Return None when marketplace URL is not configured."""
        client = BackendClient(
            installation_id="test",
            openmaskit_version="1.0.0",
            marketplace_url=None,
        )

        result = await client.get_server_info("any-id")
        assert result is None
        await client.close()


class TestVersionCheck:
    """Test the version_check endpoint."""

    @pytest.mark.anyio
    @respx.mock
    async def test_check_version_supported(self, client):
        payload = {
            "supported": True,
            "update_required": False,
            "update_available": False,
            "latest_version": "0.1.0",
        }
        route = respx.get("https://test-api.example.com/api/version_check").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await client.check_version()
        assert result == payload
        assert_catalog_request_has_no_identity(route.calls.last.request.headers)

    @pytest.mark.anyio
    @respx.mock
    async def test_check_version_update_required(self, client):
        payload = {
            "supported": False,
            "update_required": True,
            "update_available": True,
            "latest_version": "0.6.1",
        }
        respx.get("https://test-api.example.com/api/version_check").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = await client.check_version()
        assert result["update_required"] is True

    @pytest.mark.anyio
    @respx.mock
    async def test_check_version_network_error_returns_none(self, client):
        respx.get("https://test-api.example.com/api/version_check").mock(
            side_effect=httpx.TimeoutException("boom")
        )
        result = await client.check_version()
        assert result is None

    @pytest.mark.anyio
    @respx.mock
    async def test_check_version_http_error_returns_none(self, client):
        respx.get("https://test-api.example.com/api/version_check").mock(
            return_value=httpx.Response(500, text="internal")
        )
        result = await client.check_version()
        assert result is None


class TestClientLifecycle:
    """Test client lifecycle management."""

    @pytest.mark.anyio
    async def test_close_client(self):
        """Close client properly."""
        client = BackendClient(
            installation_id="test",
            openmaskit_version="1.0.0",
        )

        # Should not raise
        await client.close()

        # Client should be closed
        assert client.client.is_closed

    @pytest.mark.anyio
    async def test_client_as_context_manager(self):
        """Use client in async context (manual close)."""
        client = BackendClient(
            installation_id="test",
            openmaskit_version="1.0.0",
        )

        try:
            assert not client.client.is_closed
        finally:
            await client.close()
            assert client.client.is_closed


class TestMultipleCatalogs:
    """Several catalogs are read together, and a token stays on its own host."""

    @pytest.mark.anyio
    @respx.mock
    async def test_merges_sources_and_keeps_a_rejected_token_visible(self, tmp_path, monkeypatch):
        public = tmp_path / "catalogs.json"
        tokens = tmp_path / "tokens"
        tokens.mkdir()
        (tokens / "personal").write_text("personal-token\n")
        public.write_text(
            """
            {"catalogs": [
              {"id": "community", "name": "Community", "url": "https://catalog.example"},
              {"id": "personal", "name": "Personal", "url": "https://raw.githubusercontent.com/owner/openmaskit-catalog/main"}
            ]}
            """
        )
        monkeypatch.setenv("OPENMASKIT_MARKETPLACE_CATALOGS", str(public))
        monkeypatch.setenv("OPENMASKIT_MARKETPLACE_TOKENS_DIR", str(tokens))
        community = respx.get("https://catalog.example/api/marketplace/catalog").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"id": "one", "handle": "one", "name": "One"}],
                    "meta": {"total": 1, "page": 1, "size": 100, "total_pages": 1},
                },
            )
        )
        personal = respx.get(
            "https://raw.githubusercontent.com/owner/openmaskit-catalog/main/api/marketplace/catalog"
        ).mock(return_value=httpx.Response(401))
        other = respx.get("https://example.com/api/marketplace/catalog").mock(
            return_value=httpx.Response(200, json={"data": [], "meta": {}})
        )

        client = BackendClient(installation_id="test", openmaskit_version="1.0.0")
        result = await client.get_catalog()
        await client.close()

        assert [entry["name"] for entry in result["data"]] == ["One"]
        assert result["data"][0]["catalog_id"] == "community"
        statuses = {item["id"]: item["status"] for item in result["catalogs"]}
        assert statuses == {"community": "ok", "personal": "unauthorized"}
        assert community.calls.last.request.headers.get("authorization") is None
        assert personal.calls.last.request.headers["authorization"] == "Bearer personal-token"
        assert other.calls == []

    @pytest.mark.anyio
    @respx.mock
    async def test_server_detail_uses_the_named_catalog(self, tmp_path, monkeypatch):
        listing = tmp_path / "catalogs.json"
        listing.write_text(
            """
            {"catalogs": [
              {"id": "community", "name": "Community", "url": "https://catalog.example"},
              {"id": "personal", "name": "Personal", "url": "https://private.example"}
            ]}
            """
        )
        monkeypatch.setenv("OPENMASKIT_MARKETPLACE_CATALOGS", str(listing))
        monkeypatch.delenv("OPENMASKIT_MARKETPLACE_TOKENS_DIR", raising=False)
        respx.get("https://catalog.example/api/marketplace/servers/server-1").mock(
            return_value=httpx.Response(404)
        )
        personal = respx.get("https://private.example/api/marketplace/servers/server-1").mock(
            return_value=httpx.Response(200, json={"id": "server-1", "name": "Private"})
        )

        client = BackendClient(installation_id="test", openmaskit_version="1.0.0")
        result = await client.get_server_info("server-1", "personal")
        await client.close()

        assert result["name"] == "Private"
        assert result["catalog_id"] == "personal"
        assert personal.calls.last.request.headers.get("authorization") is None
