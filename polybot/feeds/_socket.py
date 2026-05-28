"""Shared socket tuning across feed WS connections."""
from __future__ import annotations

import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)


def enable_nodelay(ws: Any, feed_name: str) -> bool:
    """Set TCP_NODELAY on a websockets connection's underlying socket.

    Returns True iff the option was actually applied (verified via getsockopt).
    Logs at warning level on failure so silent Nagle batching surfaces.
    """
    transport = getattr(ws, "transport", None)
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:
        logger.warning("%s: socket not exposed by websockets — TCP_NODELAY skipped", feed_name)
        return False
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY) != 1:
            logger.warning("%s: TCP_NODELAY setsockopt returned 0 after set", feed_name)
            return False
        return True
    except OSError as e:
        logger.warning("%s: TCP_NODELAY failed: %s", feed_name, e)
        return False
