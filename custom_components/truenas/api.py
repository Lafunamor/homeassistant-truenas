"""TrueNAS API."""

from __future__ import annotations

import asyncio
import json
import ssl
from itertools import count
from logging import getLogger
from typing import Any
from urllib.parse import urlsplit

from homeassistant.util.ssl import client_context, client_context_no_verify
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

_LOGGER = getLogger(__name__)

DEFAULT_API_PATH = "/api/current"
DEFAULT_OPEN_TIMEOUT = 10
DEFAULT_QUERY_TIMEOUT = 30
MAX_MESSAGE_SIZE = 16777216

# Accepted host schemes and the websocket scheme they map to.
SCHEME_MAP = {
    "http": "ws",
    "ws": "ws",
    "https": "wss",
    "wss": "wss",
}

# Transport level failures that mean "nothing is answering here", after which
# it is safe to try the other scheme. TLS failures are deliberately absent: a
# certificate problem must not silently downgrade the connection to plaintext.
RETRYABLE_SCHEME_ERRORS = frozenset(
    {
        "cannot_connect",
        "connection_refused",
        "handshake_timeout",
        "http_used",
        "websocket_not_supported",
    }
)


# ---------------------------
#   has_scheme
# ---------------------------
def has_scheme(host: str) -> bool:
    """Return True when the user pinned a scheme in the host field."""
    return "://" in host.strip()


# ---------------------------
#   build_api_url
# ---------------------------
def build_api_url(host: str, use_ssl: bool = True) -> str:
    """Build the JSON-RPC websocket URL for a user supplied host.

    Accepts a bare host ("truenas.local", "10.0.0.1:8443") as well as a full
    URL ("http://10.0.0.1", "https://truenas.local/api/current"). A scheme in
    the host always wins over use_ssl, so a pasted URL does the right thing.
    """
    host = host.strip()
    scheme = "wss" if use_ssl else "ws"
    if "://" in host:
        raw_scheme, _, host = host.partition("://")
        scheme = SCHEME_MAP.get(raw_scheme.lower(), "")
        if not scheme:
            raise ValueError(f"unsupported scheme in host: {raw_scheme}")

    split = urlsplit(f"//{host.rstrip('/')}")
    if not split.hostname:
        raise ValueError(f"no host in: {host}")

    # Raises ValueError on a non numeric port.
    split.port

    path = split.path.rstrip("/") or DEFAULT_API_PATH
    return f"{scheme}://{split.netloc}{path}"


# ---------------------------
#   error_code
# ---------------------------
def error_code(exc: Exception) -> str:
    """Translate a connection exception into a config flow error code."""
    text = f"{exc.args} {exc}"
    matches = (
        ("CERTIFICATE_VERIFY_FAILED", "certificate_verify_failed"),
        ("The plain HTTP request was sent to HTTPS port", "http_used"),
        ("TLSV1_UNRECOGNIZED_NAME", "tlsv1_not_supported"),
        ("No WebSocket UPGRADE", "websocket_not_supported"),
        ("No address associated with hostname", "unknown_hostname"),
        ("Name or service not known", "invalid_hostname"),
        ("No route to host", "invalid_hostname"),
        ("Connection refused", "connection_refused"),
        ("timed out while waiting for handshake response", "handshake_timeout"),
        ("timed out", "handshake_timeout"),
        ("404", "api_not_found"),
    )
    for needle, code in matches:
        if needle in text:
            return code

    if isinstance(exc, TimeoutError):
        return "handshake_timeout"

    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"

    return "cannot_connect"


# ---------------------------
#   TrueNASAPI
# ---------------------------
class TrueNASAPI(object):
    """Handle all communication with TrueNAS."""

    def __init__(
        self,
        host: str,
        api_key: str,
        verify_ssl: bool = True,
        use_ssl: bool = True,
    ) -> None:
        """Initialize the TrueNAS API."""
        self._host = host
        self._api_key = api_key
        self._ssl_verify = verify_ssl
        self._url = build_api_url(host, use_ssl)
        self._use_tls = self._url.startswith("wss://")
        self._ssl_context: ssl.SSLContext | None = None

        self.lock = asyncio.Lock()
        self._ws: ClientConnection | None = None
        self._message_id = count(1)
        self._connected = False
        self._error = ""
        self._error_logged = False

    # ---------------------------
    #   _async_ssl_context
    # ---------------------------
    async def _async_ssl_context(self) -> ssl.SSLContext | None:
        """Return the TLS context, None for a plain websocket.

        Home Assistant caches these contexts, but building the first one reads
        the CA bundle from disk, so it is created off the event loop.
        """
        if not self._use_tls:
            return None

        if self._ssl_context is None:
            factory = client_context if self._ssl_verify else client_context_no_verify
            loop = asyncio.get_running_loop()
            self._ssl_context = await loop.run_in_executor(None, factory)

        return self._ssl_context

    # ---------------------------
    #   connect
    # ---------------------------
    async def connect(self) -> bool:
        """Return connected boolean."""
        async with self.lock:
            return await self._connect()

    # ---------------------------
    #   _connect
    # ---------------------------
    async def _connect(self) -> bool:
        """Open the connection and log in. The caller must hold the lock."""
        await self._close()
        self._error = ""
        try:
            self._ws = await ws_connect(
                self._url,
                ssl=await self._async_ssl_context(),
                max_size=MAX_MESSAGE_SIZE,
                open_timeout=DEFAULT_OPEN_TIMEOUT,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                # Never route a local NAS through a system wide proxy.
                proxy=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._error = error_code(e)
            if not self._error_logged:
                _LOGGER.error("TrueNAS %s failed to connect (%s)", self._host, e)

            self._error_logged = True
            return False

        try:
            async with asyncio.timeout(DEFAULT_QUERY_TIMEOUT):
                result = await self._request("auth.login_with_api_key", [self._api_key])

            self._connected = result is True
            if not self._connected:
                self._error = self._error or "invalid_key"
                await self._close()
        except asyncio.CancelledError:
            await self._close()
            raise
        except Exception as e:
            self._error = error_code(e)
            if not self._error_logged:
                _LOGGER.error("TrueNAS %s failed to login (%s)", self._host, e)

            self._error_logged = True
            await self._close()
            return False

        if self._connected:
            self._error_logged = False

        return self._connected

    # ---------------------------
    #   disconnect
    # ---------------------------
    async def disconnect(self) -> bool:
        """Return connected boolean."""
        async with self.lock:
            await self._close()

        return self._connected

    # ---------------------------
    #   _close
    # ---------------------------
    async def _close(self) -> None:
        """Close the websocket and reset the connection state."""
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is None:
            return

        try:
            await ws.close()
        except Exception as e:
            _LOGGER.debug("TrueNAS %s error while closing (%s)", self._host, e)

    # ---------------------------
    #   reconnect
    # ---------------------------
    async def reconnect(self) -> bool:
        """Return connected boolean."""
        async with self.lock:
            return await self._connect()

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self) -> bool:
        """Return connected boolean."""
        return self._connected

    # ---------------------------
    #   connection_test
    # ---------------------------
    async def connection_test(self) -> tuple:
        """Test connection."""
        await self.connect()
        if self.connected():
            await self.query("system.info")

        return self._connected, self._error

    # ---------------------------
    #   query
    # ---------------------------
    async def query(self, service: str, params: Any = None) -> Any:
        """Retrieve data from TrueNAS."""
        async with self.lock:
            if not self._connected and not await self._connect():
                return None

            self._error = ""
            if params is None or params == {}:
                call_params: list = []
            elif isinstance(params, list):
                call_params = params
            else:
                call_params = [params]

            _LOGGER.debug("TrueNAS %s query: %s, %s", self._host, service, call_params)
            try:
                async with asyncio.timeout(DEFAULT_QUERY_TIMEOUT):
                    data = await self._request(service, call_params)
            except asyncio.CancelledError:
                await self._close()
                raise
            except Exception as e:
                _LOGGER.warning(
                    'TrueNAS %s unable to fetch data "%s" (%s)',
                    self._host,
                    service,
                    e,
                )
                self._error = error_code(e)
                await self._close()
                return None

            _LOGGER.debug(
                "TrueNAS %s query (%s) response: %s", self._host, service, data
            )
            return data

    # ---------------------------
    #   _request
    # ---------------------------
    async def _request(self, method: str, params: list) -> Any:
        """Send a JSON-RPC request and return the matching result.

        The caller must hold the lock. Raises on transport errors so that the
        caller can drop the connection; JSON-RPC level errors are logged and
        reported as None.
        """
        if self._ws is None:
            raise ConnectionError("not connected")

        message_id = next(self._message_id)
        await self._ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "method": method,
                    "params": params,
                }
            )
        )

        data = await self._receive(message_id)
        if data is None:
            return None

        if "error" in data:
            self._log_rpc_error(method, data["error"])
            self._error = self._rpc_error_code(data["error"])
            return None

        if "result" not in data:
            _LOGGER.error(
                "TrueNAS %s query (%s) returned no result: %s",
                self._host,
                method,
                data,
            )
            self._error = "malformed_result"
            return None

        return data["result"]

    # ---------------------------
    #   _receive
    # ---------------------------
    async def _receive(self, message_id: int) -> dict | None:
        """Read messages until the response for message_id arrives."""
        assert self._ws is not None
        while True:
            message = await self._ws.recv()
            if isinstance(message, bytes):
                message = message.decode("utf-8", "replace")

            try:
                data = json.loads(message)
            except ValueError:
                _LOGGER.debug(
                    "TrueNAS %s ignoring non JSON message: %s", self._host, message
                )
                continue

            if not isinstance(data, dict):
                _LOGGER.debug(
                    "TrueNAS %s ignoring unexpected message: %s", self._host, data
                )
                continue

            # Notifications (collection_update, job progress, ...) carry no id.
            if data.get("id") != message_id:
                _LOGGER.debug(
                    "TrueNAS %s ignoring unrelated message: %s", self._host, data
                )
                continue

            return data

    # ---------------------------
    #   _log_rpc_error
    # ---------------------------
    def _log_rpc_error(self, method: str, error: Any) -> None:
        """Log a JSON-RPC error response."""
        reason = None
        if isinstance(error, dict):
            reason = (error.get("data") or {}).get("reason") or error.get("message")

        _LOGGER.error(
            "TrueNAS %s query (%s) error: %s",
            self._host,
            method,
            reason or error,
        )

    # ---------------------------
    #   _rpc_error_code
    # ---------------------------
    @staticmethod
    def _rpc_error_code(error: Any) -> str:
        """Return a short error code for a JSON-RPC error response."""
        if isinstance(error, dict):
            errname = (error.get("data") or {}).get("errname")
            if errname:
                return str(errname)

            if error.get("code") is not None:
                return str(error["code"])

        return "rpc_error"

    @property
    def url(self) -> str:
        """Return the endpoint this client talks to."""
        return self._url

    @property
    def error(self):
        """Return error."""
        return self._error
