"""
Base handler for all Tornado API endpoints.

Provides:
- JSON response helpers
- CORS headers
- Service readiness check
- Uniform error envelope
"""

import json
import time
import logging
import traceback

import tornado.web

logger = logging.getLogger("api.base")


def parse_string_field(body, field, default="", required=False, max_length=None):
    """
    Extract a string field from a parsed JSON object.

    ``get_json_body()`` guarantees a dict, but it does not constrain the value
    types. Calling ``.strip()`` on an arbitrary JSON value (for example an int)
    raises AttributeError and turns a client mistake into a 500. This helper
    gives every handler the same 400-friendly validation instead.

    Raises:
        ValueError: with a message that is safe to return to the client.
    """
    value = body.get(field, default)
    if not isinstance(value, str):
        raise ValueError(f"'{field}' must be a string.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"'{field}' field is required.")
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"'{field}' must be at most {max_length} characters.")
    return value


class BaseHandler(tornado.web.RequestHandler):
    """Base class for all REST API handlers."""

    def initialize(self, service):
        """Inject the HeadlessService instance."""
        self.service = service

    # ------------------------------------------------------------------ #
    #  CORS                                                                #
    # ------------------------------------------------------------------ #
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")
        self.set_header("Content-Type", "application/json")

    def options(self, *args, **kwargs):
        """Handle CORS preflight."""
        self.set_status(204)
        self.finish()

    # ------------------------------------------------------------------ #
    #  JSON helpers                                                        #
    # ------------------------------------------------------------------ #
    def send_success(self, data=None, status=200):
        self.set_status(status)
        self.finish(json.dumps({
            "success": True,
            "data": data,
            "error": None,
            "timestamp": time.time(),
        }))

    def send_error_response(self, message, status=400, detail=None):
        self.set_status(status)
        self.finish(json.dumps({
            "success": False,
            "data": None,
            "error": {
                "code": status,
                "message": message,
                "detail": detail,
            },
            "timestamp": time.time(),
        }))

    # ------------------------------------------------------------------ #
    #  Request body helpers                                                #
    # ------------------------------------------------------------------ #
    def get_json_body(self):
        try:
            body = json.loads(self.request.body)
        except Exception:
            return {}
        # Handlers always call .get() on the result, so a bare JSON array or
        # scalar must not leak through as a non-dict.
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------ #
    #  Service readiness guard                                             #
    # ------------------------------------------------------------------ #
    def require_ready(self):
        """Return False and send 503 if the service is not ready yet."""
        if not self.service or not self.service.ready:
            self.send_error_response(
                "Service not ready yet — HeadlessService is still initialising.",
                status=503,
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    #  Global exception handler                                            #
    # ------------------------------------------------------------------ #
    def write_error(self, status_code, **kwargs):
        exc_info = kwargs.get("exc_info")
        if exc_info:
            # Keep the traceback in the server log; do not expose internal
            # exception text to unauthenticated API clients on 5xx responses.
            logger.error("Unhandled API error (HTTP %s)", status_code, exc_info=exc_info)
        detail = None
        if exc_info and status_code < 500:
            detail = traceback.format_exception(*exc_info)[-1].strip()
        self.set_header("Content-Type", "application/json")
        self.finish(json.dumps({
            "success": False,
            "data": None,
            "error": {
                "code": status_code,
                "message": self._reason,
                "detail": detail,
            },
            "timestamp": time.time(),
        }))
