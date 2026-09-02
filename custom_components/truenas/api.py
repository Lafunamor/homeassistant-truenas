"""TrueNAS API."""

from __future__ import annotations

import json
import ssl
from inspect import signature
from itertools import count
from logging import getLogger
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from websockets.sync.client import ClientConnection, connect

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

# websockets 17.1 deprecated calling connect() outside of a context manager.
# This integration keeps a single long lived connection open, so opt into the
# documented legacy behaviour when the running version supports the flag.
_CONNECT_KWARGS: dict[str, Any] = {}
if "legacy" in signature(connect).parameters:
    _CONNECT_KWARGS["legacy"] = True


# ---------------------------
#   build_api_url
# ---------------------------
def build_api_url(host: str) -> str:
    """Build the JSON-RPC websocket URL for a user supplied host.

    Accepts a bare host ("truenas.local", "10.0.0.1:8443") as well as a full
    URL ("http://10.0.0.1", "https://truenas.local/api/current"). Hosts without
    a scheme keep the historic default of TLS.
    """
    host = host.strip()
    scheme = "wss"
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
    ) -> None:
        """Initialize the TrueNAS API."""
        self._host = host
        self._api_key = api_key
        self._ssl_verify = verify_ssl
        self._url = build_api_url(host)
        self._ssl_context: ssl.SSLContext | None = None
        if self._url.startswith("wss://"):
            self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            self._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            if verify_ssl:
                self._ssl_context.check_hostname = True
                self._ssl_context.verify_mode = ssl.CERT_REQUIRED
            else:
                self._ssl_context.check_hostname = False
                self._ssl_context.verify_mode = ssl.CERT_NONE

        # Reentrant so that query() can reconnect while holding the lock.
        self.lock = RLock()
        self._ws: ClientConnection | None = None
        self._message_id = count(1)
        self._connected = False
        self._error = ""
        self._error_logged = False

    # ---------------------------
    #   connect
    # ---------------------------
    def connect(self) -> bool:
        """Return connected boolean."""
        with self.lock:
            self._close()
            self._error = ""
            try:
                self._ws = connect(
                    self._url,
                    ssl=self._ssl_context,
                    max_size=MAX_MESSAGE_SIZE,
                    open_timeout=DEFAULT_OPEN_TIMEOUT,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    # Never route a local NAS through a system wide proxy.
                    proxy=None,
                    **_CONNECT_KWARGS,
                )
            except Exception as e:
                self._error = error_code(e)
                if not self._error_logged:
                    _LOGGER.error("TrueNAS %s failed to connect (%s)", self._host, e)

                self._error_logged = True
                return False

            try:
                result = self._request("auth.login_with_api_key", [self._api_key])
                self._connected = result is True
                if not self._connected:
                    self._error = self._error or "invalid_key"
                    self._close()
            except Exception as e:
                self._error = error_code(e)
                if not self._error_logged:
                    _LOGGER.error("TrueNAS %s failed to login (%s)", self._host, e)

                self._error_logged = True
                self._close()
                return False

            if self._connected:
                self._error_logged = False

            return self._connected

    # ---------------------------
    #   disconnect
    # ---------------------------
    def disconnect(self) -> bool:
        """Return connected boolean."""
        with self.lock:
            self._close()

        return self._connected

    # ---------------------------
    #   _close
    # ---------------------------
    def _close(self) -> None:
        """Close the websocket and reset the connection state."""
        ws, self._ws = self._ws, None
        self._connected = False
        if ws is None:
            return

        try:
            ws.close()
        except Exception as e:
            _LOGGER.debug("TrueNAS %s error while closing (%s)", self._host, e)

    # ---------------------------
    #   reconnect
    # ---------------------------
    def reconnect(self) -> bool:
        """Return connected boolean."""
        with self.lock:
            self.disconnect()
            return self.connect()

    # ---------------------------
    #   connected
    # ---------------------------
    def connected(self) -> bool:
        """Return connected boolean."""
        return self._connected

    # ---------------------------
    #   connection_test
    # ---------------------------
    def connection_test(self) -> tuple:
        """Test connection."""
        self.connect()
        if self.connected():
            self.query("system.info")

        return self._connected, self._error

    # ---------------------------
    #   query
    # ---------------------------
    def query(self, service: str, params: Any = None) -> Any:
        """Retrieve data from TrueNAS."""
        with self.lock:
            if not self._connected and not self.connect():
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
                data = self._request(service, call_params)
            except Exception as e:
                _LOGGER.warning(
                    'TrueNAS %s unable to fetch data "%s" (%s)',
                    self._host,
                    service,
                    e,
                )
                self._error = error_code(e)
                self._close()
                return None

            _LOGGER.debug(
                "TrueNAS %s query (%s) response: %s", self._host, service, data
            )
            return data

    # ---------------------------
    #   _request
    # ---------------------------
    def _request(self, method: str, params: list) -> Any:
        """Send a JSON-RPC request and return the matching result.

        The caller must hold the lock. Raises on transport errors so that
        query() can drop the connection; JSON-RPC level errors are logged and
        reported as None.
        """
        if self._ws is None:
            raise ConnectionError("not connected")

        message_id = next(self._message_id)
        self._ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "method": method,
                    "params": params,
                }
            )
        )

        data = self._receive(message_id)
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
    def _receive(self, message_id: int) -> dict | None:
        """Read messages until the response for message_id arrives."""
        while True:
            message = self._ws.recv(timeout=DEFAULT_QUERY_TIMEOUT)
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
    def error(self):
        """Return error."""
        return self._error
