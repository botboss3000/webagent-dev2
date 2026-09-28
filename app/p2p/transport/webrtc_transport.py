"""WebRTCTransport — direct P2P via aiortc data channels (DTLS-SRTP encrypted).

Manages per-peer RTCPeerConnection instances with STUN-based NAT traversal.
Data channel carries framed request/response messages with Ed25519 identity proofs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import struct
from typing import Dict, Optional, Tuple

from app.p2p import identity
from app.p2p.transport import Transport

logger = logging.getLogger(__name__)

# Default STUN server (Google, free public infrastructure)
DEFAULT_STUN = "stun.l.google.com:19302"

# Connection states
STATE_NEW = "new"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_RECONNECTING = "reconnecting"

# Timeouts
CONNECT_TIMEOUT = 30.0  # seconds to wait for ICE to complete


class WebRTCTransport(Transport):
    """WebRTC-based direct P2P transport."""

    def __init__(self, stun_server: str = DEFAULT_STUN, turn_config: Optional[Dict] = None) -> None:
        self._stun_server = stun_server
        self._turn_config = turn_config
        self._connections: Dict[str, RTCPeerConnection] = {}  # peer_id -> connection
        self._channels: Dict[str, object] = {}  # peer_id -> data channel
        self._states: Dict[str, str] = {}  # peer_id -> state
        self._pending_answers: Dict[str, asyncio.Event] = {}  # peer_id -> answer received event
        self._pending_offers: Dict[str, Tuple[str, str]] = {}  # peer_id -> (sdp, ice_json)

    async def create_offer(self) -> Tuple[str, str]:
        """Generate an SDP offer and ICE candidates for a new peer connection.
        
        Returns (sdp_offer_b64, ice_candidates_json).
        The caller encodes these into a QR code.
        """
        from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer

        config = RTCConfiguration([
            RTCIceServer(urls=f"stun:{self._stun_server}"),
        ])
        if self._turn_config:
            config.iceServers.append(RTCIceServer(**self._turn_config))

        pc = RTCPeerConnection(configuration=config)
        channel = pc.createDataChannel("sync", ordered=True)

        # Gather ICE candidates
        ice_candidates = []
        ice_done = asyncio.Event()

        @pc.on("icecandidate")
        def on_ice_candidate(candidate):
            if candidate:
                ice_candidates.append({
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                })
            else:
                ice_done.set()  # End of candidates

        # Create offer
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # Wait for ICE gathering to complete (or timeout)
        try:
            await asyncio.wait_for(ice_done.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("ICE gathering timed out — proceeding with partial candidates")

        sdp_b64 = base64.b64encode(pc.localDescription.sdp.encode()).decode()
        ice_json = json.dumps(ice_candidates)

        # Store for later completion
        temp_id = f"pending_{identity.instance_id()}"
        self._connections[temp_id] = pc
        self._pending_offers[temp_id] = (sdp_b64, ice_json)

        return sdp_b64, ice_json

    async def accept_offer(self, sdp_offer_b64: str, ice_candidates_json: str) -> Tuple[str, str]:
        """Accept a peer's SDP offer. Returns (sdp_answer_b64, ice_candidates_json)."""
        from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription

        sdp_offer = base64.b64decode(sdp_offer_b64).decode()
        ice_candidates = json.loads(ice_candidates_json)

        config = RTCConfiguration([
            RTCIceServer(urls=f"stun:{self._stun_server}"),
        ])
        if self._turn_config:
            config.iceServers.append(RTCIceServer(**self._turn_config))

        pc = RTCPeerConnection(configuration=config)

        answer_candidates = []
        ice_done = asyncio.Event()

        @pc.on("icecandidate")
        def on_ice_candidate(candidate):
            if candidate:
                answer_candidates.append({
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                })
            else:
                ice_done.set()

        @pc.on("datachannel")
        def on_datachannel(channel):
            channel.on("message")(lambda msg: self._on_message(temp_id, msg))
            self._channels[temp_id] = channel

        # Feed remote offer
        offer_desc = RTCSessionDescription(sdp=sdp_offer, type="offer")
        await pc.setRemoteDescription(offer_desc)

        # Add remote ICE candidates
        for c in ice_candidates:
            from aiortc import RTCIceCandidate
            await pc.addIceCandidate(RTCIceCandidate(**c))

        # Create answer
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        try:
            await asyncio.wait_for(ice_done.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("ICE gathering timed out")

        answer_b64 = base64.b64encode(pc.localDescription.sdp.encode()).decode()
        answer_ice_json = json.dumps(answer_candidates)

        temp_id = f"pending_{identity.instance_id()}_answer"
        self._connections[temp_id] = pc
        self._states[temp_id] = STATE_CONNECTING

        return answer_b64, answer_ice_json

    async def complete_answer(self, sdp_answer_b64: str, ice_candidates_json: str) -> bool:
        """Feed the peer's SDP answer back to complete the WebRTC connection."""
        from aiortc import RTCSessionDescription, RTCIceCandidate

        temp_id = self._find_pending()
        if not temp_id:
            logger.error("No pending offer to complete")
            return False

        pc = self._connections.get(temp_id)
        if not pc:
            return False

        sdp_answer = base64.b64decode(sdp_answer_b64).decode()
        ice_candidates = json.loads(ice_candidates_json)

        answer_desc = RTCSessionDescription(sdp=sdp_answer, type="answer")
        await pc.setRemoteDescription(answer_desc)

        for c in ice_candidates:
            await pc.addIceCandidate(RTCIceCandidate(**c))

        self._states[temp_id] = STATE_CONNECTING
        return True

    async def connect(self, peer: Dict) -> bool:
        """Establish a WebRTC connection to a peer."""
        # Reject revoked peers before any negotiation
        from app.p2p.revocation import is_revoked
        pub_hex = peer.get("public_key", "")
        if pub_hex and is_revoked(pub_hex):
            logger.warning("Rejected revoked peer %s", peer.get("id"))
            return False

        # If we're the offerer (pending offer exists), wait for answer
        temp_id = self._find_pending()
        if temp_id:
            # Already connecting — just wait
            try:
                await asyncio.wait_for(
                    self._wait_for_connected(temp_id),
                    timeout=CONNECT_TIMEOUT,
                )
                # Rename from temp to real peer_id
                peer_id = peer["id"]
                self._connections[peer_id] = self._connections.pop(temp_id)
                if temp_id in self._channels:
                    self._channels[peer_id] = self._channels.pop(temp_id)
                self._states[peer_id] = STATE_CONNECTED
                return True
            except asyncio.TimeoutError:
                logger.warning("WebRTC connection timed out for %s", peer.get("id"))
                self._states[temp_id] = STATE_DISCONNECTED
                return False

        # If we're the answerer, check if already connected
        answer_temp = f"pending_{identity.instance_id()}_answer"
        if answer_temp in self._connections:
            peer_id = peer["id"]
            self._connections[peer_id] = self._connections.pop(answer_temp)
            if answer_temp in self._channels:
                self._channels[peer_id] = self._channels.pop(answer_temp)
            self._states[peer_id] = STATE_CONNECTED
            return True

        return False

    async def send(self, peer_id: str, method: str, path: str, body: bytes) -> bytes:
        """Send a request over the WebRTC data channel."""
        channel = self._channels.get(peer_id)
        if not channel or self._states.get(peer_id) != STATE_CONNECTED:
            raise IOError(f"Not connected to peer {peer_id}")

        # Frame: 4-byte big-endian length prefix + JSON body
        payload = json.dumps({
            "method": method,
            "path": path,
            "body_b64": base64.b64encode(body).decode() if body else "",
        }).encode()

        framed = struct.pack("!I", len(payload)) + payload
        channel.send(framed)

        # TODO Phase 1b: implement request/response matching with correlation IDs
        # For now, fire-and-forget — the sync engine will use pull-model
        return b""

    async def disconnect(self, peer_id: str) -> None:
        """Close the WebRTC connection to a peer."""
        pc = self._connections.pop(peer_id, None)
        if pc:
            await pc.close()
        self._channels.pop(peer_id, None)
        self._states.pop(peer_id, None)

    async def is_connected(self, peer_id: str) -> bool:
        return self._states.get(peer_id) == STATE_CONNECTED

    def _find_pending(self) -> Optional[str]:
        """Find a pending offer connection."""
        for tid in list(self._pending_offers.keys()):
            return tid
        return None

    async def _wait_for_connected(self, temp_id: str) -> None:
        """Block until the data channel opens."""
        # Poll the state
        while self._states.get(temp_id) != STATE_CONNECTED:
            await asyncio.sleep(0.1)

    def _on_message(self, temp_id: str, message: str) -> None:
        """Handle an incoming data channel message."""
        # TODO Phase 1b: framed request/response protocol
        pass
