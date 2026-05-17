"""
NSIGII Peer-to-Peer Network Layer
OBINexus / NSIGII Constitutional Computing Framework

Topology: X ---O--- X   (peer to peer, two nodes)

Connection lifecycle:
  1. TCP connect
  2. MMUKO calibration handshake  (NOISE -> NONOISE -> NOSIGNAL -> SIGNAL)
  3. Fingerprint exchange and verification
  4. Live data exchange
  5. Graceful disconnect

Framed message format:
  MAGIC(8) | TYPE(1) | LEN(4) | PAYLOAD(LEN)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from mmuko_connect_calibration import (
    ByteState,
    CalibrationEvent,
    CalibrationTuple,
    Receiver,
    Transmitter,
    Verifier,
)

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

NSIGII_PORT    = 9200
NSIGII_MAGIC   = b"NSIGII\x00\x01"   # 8-byte magic header

MSG_CALIBRATE   = 0x01
MSG_FINGERPRINT = 0x02
MSG_ACK         = 0x03
MSG_DATA        = 0x04
MSG_DISCONNECT  = 0x05

HEADER_SIZE = len(NSIGII_MAGIC) + 1 + 4   # 13 bytes

logger = logging.getLogger("nsigii.peer")


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------

def _pack_message(msg_type: int, payload: bytes) -> bytes:
    """Frame: MAGIC(8) + TYPE(1) + LEN(4) + PAYLOAD"""
    return NSIGII_MAGIC + struct.pack(">BI", msg_type, len(payload)) + payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes, blocking until available."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection closed by remote peer")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(sock: socket.socket) -> tuple:
    """Read one framed message.  Returns (msg_type, payload)."""
    raw_header = _recv_exact(sock, HEADER_SIZE)
    magic   = raw_header[:8]
    if magic != NSIGII_MAGIC:
        raise ValueError("Bad NSIGII magic: {!r}".format(magic))
    msg_type = raw_header[8]
    length   = struct.unpack(">I", raw_header[9:13])[0]
    payload  = _recv_exact(sock, length) if length else b""
    return msg_type, payload


# ---------------------------------------------------------------------------
# Peer info
# ---------------------------------------------------------------------------

@dataclass
class PeerInfo:
    host:        str
    port:        int
    node_id:     str  = ""
    fingerprint: str  = ""
    connected:   bool = False
    sock:        Optional[socket.socket] = field(default=None, repr=False)

    @property
    def key(self) -> str:
        return "{0}:{1}".format(self.host, self.port)


# ---------------------------------------------------------------------------
# NSIGIIPeer
# ---------------------------------------------------------------------------

class NSIGIIPeer:
    """
    NSIGII peer node.

    Listens for incoming TCP connections (server role) and can also
    initiate outgoing TCP connections (client role).  Uses MMUKO
    CalibrationSession as the handshake protocol on every new connection.
    """

    def __init__(self, node_id: str, listen_port: int = NSIGII_PORT):
        self.node_id     = node_id
        self.listen_port = listen_port

        self._peers:         dict         = {}
        self._lock           = threading.Lock()
        self._running        = False
        self._server_sock    = None
        self._server_thread  = None

        self.on_peer_connected    = None
        self.on_peer_disconnected = None
        self.on_data              = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the TCP listener in a background daemon thread."""
        self._running = True
        self._server_thread = threading.Thread(
            target=self._serve, daemon=True, name="nsigii-server"
        )
        self._server_thread.start()
        logger.info("[%s] Listening on port %d", self.node_id, self.listen_port)

    def stop(self):
        """Signal all threads to stop and close the server socket."""
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        with self._lock:
            for peer in list(self._peers.values()):
                try:
                    peer.sock.sendall(_pack_message(MSG_DISCONNECT, b""))
                    peer.sock.close()
                except OSError:
                    pass
        logger.info("[%s] Stopped", self.node_id)

    # ------------------------------------------------------------------
    # Server: accept incoming connections
    # ------------------------------------------------------------------

    def _serve(self):
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self.listen_port))
        self._server_sock.listen(5)
        while self._running:
            try:
                conn, addr = self._server_sock.accept()
                threading.Thread(
                    target=self._handle_incoming,
                    args=(conn, addr),
                    daemon=True,
                    name="nsigii-rx-{0}".format(addr[0]),
                ).start()
            except OSError:
                break

    def _handle_incoming(self, conn, addr):
        host, port = addr
        logger.info("[%s] Incoming connection from %s:%d", self.node_id, host, port)
        try:
            peer = self._calibrate_as_receiver(conn, host, port)
            if peer:
                self._register_peer(peer)
                self._read_loop(peer)
            else:
                logger.warning("[%s] Calibration rejected from %s:%d", self.node_id, host, port)
        except Exception as exc:
            logger.error("[%s] Error handling %s:%d -- %s", self.node_id, host, port, exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Client: initiate outgoing connection
    # ------------------------------------------------------------------

    def connect_to(self, remote_host: str, remote_port: int = NSIGII_PORT) -> bool:
        """
        Initiate a TCP connection to a remote peer and run the MMUKO
        calibration handshake.  Returns True on success.
        """
        key = "{0}:{1}".format(remote_host, remote_port)
        with self._lock:
            if key in self._peers and self._peers[key].connected:
                logger.debug("[%s] Already connected to %s", self.node_id, key)
                return True
        try:
            sock = socket.create_connection((remote_host, remote_port), timeout=10)
            sock.settimeout(None)    # connect timeout only; switch to blocking for data
            peer = self._calibrate_as_transmitter(sock, remote_host, remote_port)
            if peer:
                self._register_peer(peer)
                threading.Thread(
                    target=self._read_loop,
                    args=(peer,),
                    daemon=True,
                    name="nsigii-tx-{0}".format(remote_host),
                ).start()
                return True
            else:
                sock.close()
                return False
        except Exception as exc:
            logger.error("[%s] connect_to %s failed -- %s", self.node_id, key, exc)
            return False

    # ------------------------------------------------------------------
    # MMUKO frame builder
    # ------------------------------------------------------------------

    @staticmethod
    def _make_signal_frames(node_id: str) -> bytes:
        """
        Build a calibration stream of exactly 16-byte frames, each
        starting with the 0xAA 0x55 preamble.

        Every 16-byte window aligns with a frame, so every window
        starts with the preamble -> structure_score gets the +0.3 boost
        -> classified as SIGNAL.  This ensures SIGNAL is dominant in the
        calibration vector on both sides of the handshake.

        Frame layout (16 bytes):
          [0xAA][0x55][14 bytes of ASCII tag, right-padded with ':']
        """
        def _frame(tag):
            body = tag.encode("ascii", errors="replace")[:14].ljust(14, b":")
            return bytes([0xAA, 0x55]) + body

        safe_id = node_id[:10].ljust(10, "-")
        return (
            _frame("NSIGII:{0}".format(safe_id))
            + _frame("CONNECT:v1:::::")
            + _frame("READY:::::::::")
        )

    # ------------------------------------------------------------------
    # MMUKO Calibration Handshake -- Transmitter side (TX initiates)
    # ------------------------------------------------------------------

    def _calibrate_as_transmitter(self, sock, host, port):
        """
        TX side of the MMUKO handshake.

        Sends three 16-byte signal frames (each starting with 0xAA 0x55).
        All classification windows are SIGNAL -> SIGNAL dominant -> valid.
        """
        logger.info("[%s] MMUKO handshake (TX) -> %s:%d", self.node_id, host, port)

        combined = self._make_signal_frames(self.node_id)
        sock.sendall(_pack_message(MSG_CALIBRATE, combined))

        msg_type, payload = _recv_message(sock)
        if msg_type != MSG_FINGERPRINT:
            logger.warning("[%s] Expected MSG_FINGERPRINT, got %d", self.node_id, msg_type)
            return None

        resp = json.loads(payload.decode())
        remote_fp      = resp.get("fingerprint", "")
        remote_node_id = resp.get("node_id", "{0}:{1}".format(host, port))
        rx_valid       = resp.get("valid", False)

        cal    = CalibrationTuple()
        states = cal.classify_stream(combined, window_size=16)
        tx_valid = (max(set(states), key=states.count) == ByteState.SIGNAL)

        if rx_valid and tx_valid:
            logger.info("[%s] Calibration SUCCESS with %s -- fp=%s",
                        self.node_id, remote_node_id, remote_fp)
            sock.sendall(_pack_message(MSG_ACK, b"OK"))
            return PeerInfo(
                host=host, port=port,
                node_id=remote_node_id,
                fingerprint=remote_fp,
                connected=True,
                sock=sock,
            )
        else:
            logger.warning("[%s] Calibration FAILED with %s -- rx_valid=%s tx_valid=%s",
                           self.node_id, remote_node_id, rx_valid, tx_valid)
            sock.sendall(_pack_message(MSG_ACK, b"FAIL"))
            return None

    # ------------------------------------------------------------------
    # MMUKO Calibration Handshake -- Receiver side (RX waits)
    # ------------------------------------------------------------------

    def _calibrate_as_receiver(self, sock, host, port):
        """
        RX side of the MMUKO handshake.

        Receives the combined calibration byte stream, classifies it
        window-by-window, verifies, sends fingerprint + validity back,
        and waits for ACK.
        """
        logger.info("[%s] MMUKO handshake (RX) from %s:%d", self.node_id, host, port)

        msg_type, payload = _recv_message(sock)
        if msg_type != MSG_CALIBRATE:
            logger.warning("[%s] Expected MSG_CALIBRATE, got %d", self.node_id, msg_type)
            return None

        rx  = Receiver(node_id=self.node_id)
        vrf = Verifier(node_id="{0}-VRF".format(self.node_id))

        rx._connected = True
        evt = CalibrationEvent(kind="data", payload=payload,
                               node_id="{0}:{1}".format(host, port))
        rx.receive(evt)

        is_valid, fingerprint = vrf.verify(rx)

        resp = json.dumps({
            "node_id":     self.node_id,
            "fingerprint": fingerprint,
            "valid":       is_valid,
        }).encode()
        sock.sendall(_pack_message(MSG_FINGERPRINT, resp))

        msg_type, ack = _recv_message(sock)
        if msg_type == MSG_ACK and ack == b"OK":
            logger.info("[%s] Calibration SUCCESS from %s:%d -- fp=%s",
                        self.node_id, host, port, fingerprint)
            return PeerInfo(
                host=host, port=port,
                node_id="{0}:{1}".format(host, port),
                fingerprint=fingerprint,
                connected=True,
                sock=sock,
            )
        logger.warning("[%s] Remote rejected calibration from %s:%d",
                       self.node_id, host, port)
        return None

    # ------------------------------------------------------------------
    # Peer registry and data read loop
    # ------------------------------------------------------------------

    def _register_peer(self, peer):
        with self._lock:
            self._peers[peer.key] = peer
        if self.on_peer_connected:
            try:
                self.on_peer_connected(peer)
            except Exception as exc:
                logger.error("on_peer_connected callback error: %s", exc)

    def _read_loop(self, peer):
        """Continuously read framed messages from a connected peer."""
        sock = peer.sock
        try:
            while self._running and peer.connected:
                msg_type, payload = _recv_message(sock)
                if msg_type == MSG_DATA:
                    if self.on_data:
                        try:
                            self.on_data(peer, payload)
                        except Exception as exc:
                            logger.error("on_data callback error: %s", exc)
                    else:
                        logger.info("[%s] DATA from %s: %r",
                                    self.node_id, peer.key, payload[:64])
                elif msg_type == MSG_DISCONNECT:
                    logger.info("[%s] Peer %s sent DISCONNECT",
                                self.node_id, peer.key)
                    break
                else:
                    logger.warning("[%s] Unknown msg_type=%d from %s",
                                   self.node_id, msg_type, peer.key)
        except Exception as exc:
            logger.warning("[%s] Read loop ended for %s -- %s",
                           self.node_id, peer.key, exc)
        finally:
            peer.connected = False
            with self._lock:
                self._peers.pop(peer.key, None)
            try:
                sock.close()
            except OSError:
                pass
            if self.on_peer_disconnected:
                try:
                    self.on_peer_disconnected(peer)
                except Exception as exc:
                    logger.error("on_peer_disconnected callback error: %s", exc)

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    def send(self, peer_key: str, data: bytes) -> bool:
        """Send application data to a connected peer by its 'host:port' key."""
        with self._lock:
            peer = self._peers.get(peer_key)
        if not peer or not peer.connected:
            logger.warning("[%s] Cannot send to %s -- not connected",
                           self.node_id, peer_key)
            return False
        try:
            peer.sock.sendall(_pack_message(MSG_DATA, data))
            return True
        except Exception as exc:
            logger.error("[%s] Send error to %s -- %s", self.node_id, peer_key, exc)
            return False

    def broadcast(self, data: bytes) -> int:
        """Send data to all connected peers.  Returns number of successful sends."""
        with self._lock:
            keys = list(self._peers.keys())
        return sum(1 for k in keys if self.send(k, data))

    @property
    def connected_peers(self):
        with self._lock:
            return [p for p in self._peers.values() if p.connected]
