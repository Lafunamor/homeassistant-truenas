"""Tests for the TrueNAS websocket API client."""

from __future__ import annotations

import json
import ssl

import pytest

from custom_components.truenas.api import (
    TrueNASAPI,
    build_api_url,
    error_code,
)


# ---------------------------
#   build_api_url
# ---------------------------
@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("10.0.0.1", "wss://10.0.0.1/api/current"),
        ("10.0.0.1:8443", "wss://10.0.0.1:8443/api/current"),
        ("truenas.local ", "wss://truenas.local/api/current"),
        ("https://truenas.local", "wss://truenas.local/api/current"),
        ("HTTPS://truenas.local", "wss://truenas.local/api/current"),
        ("wss://truenas.local", "wss://truenas.local/api/current"),
        ("http://10.0.0.1", "ws://10.0.0.1/api/current"),
        ("http://10.0.0.1/", "ws://10.0.0.1/api/current"),
        ("ws://10.0.0.1:8080", "ws://10.0.0.1:8080/api/current"),
        ("https://truenas.local/api/current", "wss://truenas.local/api/current"),
        (
            "https://truenas.local/proxy/api/current",
            "wss://truenas.local/proxy/api/current",
        ),
    ],
)
def test_build_api_url(host: str, expected: str) -> None:
    """A host with or without scheme, port or path resolves to a websocket URL."""
    assert build_api_url(host) == expected


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("10.0.0.1", "ws://10.0.0.1/api/current"),
        ("10.0.0.1:8080", "ws://10.0.0.1:8080/api/current"),
        # An explicit scheme always wins over the toggle.
        ("https://truenas.local", "wss://truenas.local/api/current"),
    ],
)
def test_build_api_url_without_ssl(host: str, expected: str) -> None:
    """Turning HTTPS off switches the default scheme to ws://."""
    assert build_api_url(host, use_ssl=False) == expected


@pytest.mark.parametrize("host", ["", "   ", "ftp://truenas.local", "http://"])
def test_build_api_url_invalid(host: str) -> None:
    """An unusable host is rejected instead of building a broken URL."""
    with pytest.raises(ValueError):
        build_api_url(host)


# ---------------------------
#   error_code
# ---------------------------
@pytest.mark.parametrize(
    ("exception", "expected"),
    [
        (ConnectionRefusedError(111, "Connection refused"), "connection_refused"),
        (OSError("[Errno -2] Name or service not known"), "invalid_hostname"),
        (TimeoutError("timed out during opening handshake"), "handshake_timeout"),
        (Exception("CERTIFICATE_VERIFY_FAILED"), "certificate_verify_failed"),
        (Exception("something else entirely"), "cannot_connect"),
    ],
)
def test_error_code(exception: Exception, expected: str) -> None:
    """Connection exceptions map onto config flow error codes."""
    assert error_code(exception) == expected


# ---------------------------
#   FakeWebsocket
# ---------------------------
class FakeWebsocket:
    """Minimal stand-in for websockets.asyncio.client.ClientConnection."""

    def __init__(self, responses: list) -> None:
        """Store the canned responses, keyed by call order."""
        self.responses = responses
        self.sent: list[dict] = []
        self.inbox: list = []
        self.closed = False

    async def send(self, message: str) -> None:
        """Record the request and queue the matching canned response."""
        payload = json.loads(message)
        self.sent.append(payload)
        response = self.responses.pop(0)
        if callable(response):
            response = response(payload)
        if isinstance(response, list):
            self.inbox.extend(response)
        else:
            self.inbox.append(response)

    async def recv(self, decode: bool | None = None) -> str:
        """Return the next queued message."""
        if not self.inbox:
            raise ConnectionError("no more messages")
        message = self.inbox.pop(0)
        if isinstance(message, Exception):
            raise message
        return json.dumps(message) if not isinstance(message, str) else message

    async def close(self) -> None:
        """Mark the connection as closed."""
        self.closed = True


def _login_ok(payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": payload["id"], "result": True}


@pytest.fixture(name="connect_factory")
def connect_factory_fixture(monkeypatch):
    """Patch the websocket connect() used by the API client."""

    created: list[FakeWebsocket] = []

    def _install(responses: list):
        async def _connect(*args, **kwargs):
            websocket = FakeWebsocket(list(responses))
            created.append(websocket)
            return websocket

        monkeypatch.setattr("custom_components.truenas.api.ws_connect", _connect)
        return created

    return _install


# ---------------------------
#   query
# ---------------------------
async def test_query_returns_result(connect_factory) -> None:
    """A successful call returns the JSON-RPC result payload."""
    connect_factory(
        [
            _login_ok,
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": {"version": "25.10"}},
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    assert await api.connect() is True
    assert await api.query("system.info") == {"version": "25.10"}
    assert api.error == ""


async def test_query_ignores_unrelated_messages(connect_factory) -> None:
    """Notifications and stale replies do not desynchronise the connection."""
    connect_factory(
        [
            _login_ok,
            lambda p: [
                {"jsonrpc": "2.0", "method": "collection_update", "params": {}},
                {"jsonrpc": "2.0", "id": p["id"] + 99, "result": "stale"},
                {"jsonrpc": "2.0", "id": p["id"], "result": ["expected"]},
            ],
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    assert await api.connect() is True
    assert await api.query("pool.query") == ["expected"]


async def test_query_error_response_returns_none(connect_factory) -> None:
    """A JSON-RPC error is reported as no data instead of a raw envelope."""
    connect_factory(
        [
            _login_ok,
            lambda p: {
                "jsonrpc": "2.0",
                "id": p["id"],
                "error": {
                    "code": -32001,
                    "message": "Method call error",
                    "data": {"errname": "ENOMETHOD", "reason": "Method does not exist"},
                },
            },
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    assert await api.connect() is True
    assert await api.query("reporting.get_data") is None
    assert api.error == "ENOMETHOD"


async def test_query_error_keeps_connection(connect_factory) -> None:
    """A method level error must not tear down a healthy connection."""
    created = connect_factory(
        [
            _login_ok,
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "error": {"code": -32001}},
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": "second call"},
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    await api.connect()
    assert await api.query("broken.method") is None
    assert api.connected() is True
    assert await api.query("system.info") == "second call"
    assert len(created) == 1


async def test_query_transport_error_disconnects(connect_factory) -> None:
    """A transport failure drops the connection so the next call reconnects."""
    created = connect_factory(
        [
            _login_ok,
            lambda p: ConnectionError("connection closed"),
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    await api.connect()
    assert await api.query("system.info") is None
    assert api.connected() is False
    assert api.error == "cannot_connect"
    assert created[0].closed is True


async def test_invalid_api_key(connect_factory) -> None:
    """A rejected API key is reported and the socket is closed."""
    created = connect_factory(
        [lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": False}]
    )
    api = TrueNASAPI("10.0.0.1", "bad-key", False)
    assert await api.connect() is False
    assert api.error == "invalid_key"
    assert created[0].closed is True


async def test_disconnect_closes_and_clears(connect_factory) -> None:
    """disconnect() closes the socket and allows a later reconnect."""
    created = connect_factory([_login_ok, _login_ok])
    api = TrueNASAPI("10.0.0.1", "key", False)
    await api.connect()
    await api.disconnect()
    assert created[0].closed is True
    assert api.connected() is False
    assert await api.connect() is True
    assert len(created) == 2


async def test_query_params_are_wrapped(connect_factory) -> None:
    """Scalar and mapping params are always sent as a JSON-RPC params list."""
    created = connect_factory(
        [
            _login_ok,
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": None},
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": None},
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": None},
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    await api.connect()
    await api.query("no.params")
    await api.query("dict.params", {"a": 1})
    await api.query("list.params", [1, 2])
    sent = created[0].sent
    assert sent[1]["params"] == []
    assert sent[2]["params"] == [{"a": 1}]
    assert sent[3]["params"] == [1, 2]


async def test_request_ids_are_unique(connect_factory) -> None:
    """Every request uses a fresh id so replies can be matched."""
    created = connect_factory(
        [
            _login_ok,
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": 1},
            lambda p: {"jsonrpc": "2.0", "id": p["id"], "result": 2},
        ]
    )
    api = TrueNASAPI("10.0.0.1", "key", False)
    await api.connect()
    await api.query("a")
    await api.query("b")
    ids = [message["id"] for message in created[0].sent]
    assert len(set(ids)) == len(ids)


async def test_tls_context_only_for_secure_websocket() -> None:
    """A plain ws:// host must not be given a TLS context."""
    assert (
        await TrueNASAPI("http://10.0.0.1", "key", False)._async_ssl_context() is None
    )
    assert (
        await TrueNASAPI("10.0.0.1", "key", False, use_ssl=False)._async_ssl_context()
        is None
    )
    assert await TrueNASAPI("10.0.0.1", "key", False)._async_ssl_context() is not None


async def test_verified_context_has_a_trust_store() -> None:
    """Verifying certificates needs CA certificates to verify against."""
    context = await TrueNASAPI("10.0.0.1", "key", True)._async_ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs(), "no CA certificates loaded"
