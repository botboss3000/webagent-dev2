"""P2P transport layer — pluggable backends for moving bytes between peers.

Two backends:
  HttpTransport    — Ed25519-signed + ECDH-encrypted HTTP requests
  WebRTCTransport  — Direct P2P via aiortc data channels (DTLS-SRTP encrypted)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict


class Transport(ABC):
    """Interface for sending authenticated+encrypted requests to a peer."""

    @abstractmethod
    async def connect(self, peer: Dict) -> bool:
        """Establish a connection to a peer. Returns True on success."""
        ...

    @abstractmethod
    async def send(self, peer_id: str, method: str, path: str, body: bytes) -> bytes:
        """Send a request and return the response body. Raises on failure."""
        ...

    @abstractmethod
    async def disconnect(self, peer_id: str) -> None:
        """Tear down a peer connection."""
        ...

    @abstractmethod
    async def is_connected(self, peer_id: str) -> bool:
        """True if the peer currently has an open connection."""
        ...
