"""
MMUKO Calibration Sequence
OBINexus / NSIGII Constitutional Computing Framework
"""
from __future__ import annotations
import os, time, hashlib, enum, math
from dataclasses import dataclass, field
from typing import Optional

class ByteState(enum.IntEnum):
    NOISE    = 0
    NONOISE  = 1
    SIGNAL   = 2
    NOSIGNAL = 3

@dataclass
class CalibrationTuple:
    noise_threshold:  float = 0.7
    signal_threshold: float = 0.6
    silence_window:   int   = 8

    def _entropy_score(self, window: bytes) -> float:
        if not window: return 0.0
        counts = [0]*256
        for b in window: counts[b] += 1
        n = len(window); score = 0.0
        for c in counts:
            if c > 0:
                p = c/n; score -= p*math.log2(p)
        return score/8.0

    def _structure_score(self, window: bytes) -> float:
        if not window: return 0.0
        unique = len(set(window))
        structure = 1.0 - (unique/max(len(window),1))
        if len(window)>=2 and window[0]==0xAA and window[1]==0x55:
            structure = min(1.0, structure+0.3)
        return structure

    def classify(self, window: bytes) -> ByteState:
        if not window: return ByteState.NOSIGNAL
        if all(b==0x00 for b in window): return ByteState.NOSIGNAL
        e = self._entropy_score(window); s = self._structure_score(window)
        if e >= self.noise_threshold: return ByteState.NOISE
        if s >= self.signal_threshold: return ByteState.SIGNAL
        return ByteState.NONOISE

    def classify_stream(self, stream: bytes, window_size: int=16) -> list:
        return [self.classify(stream[i:i+window_size]) for i in range(0,len(stream),window_size)]

@dataclass
class CalibrationEvent:
    kind:      str
    payload:   bytes = b""
    timestamp: float = field(default_factory=time.time)
    node_id:   str   = ""

@dataclass
class Transmitter:
    node_id: str = "TX-001"
    PREAMBLE = bytes([0xAA, 0x55])
    def emit(self, payload: bytes) -> CalibrationEvent:
        return CalibrationEvent(kind="data", payload=self.PREAMBLE+payload, node_id=self.node_id)
    def connect(self) -> CalibrationEvent:
        return CalibrationEvent(kind="connect", node_id=self.node_id)
    def disconnect(self) -> CalibrationEvent:
        return CalibrationEvent(kind="disconnect", node_id=self.node_id)

@dataclass
class Receiver:
    calibrator: CalibrationTuple = field(default_factory=CalibrationTuple)
    node_id:    str = "RX-001"
    _vector:    list = field(default_factory=list, init=False)
    _connected: bool = field(default=False, init=False)

    def receive(self, event: CalibrationEvent) -> list:
        if event.kind == "connect":
            self._connected = True; return []
        if event.kind == "disconnect":
            self._connected = False; return []
        if not self._connected: return []
        new = self.calibrator.classify_stream(event.payload)
        self._vector.extend(new); return new

    @property
    def calibration_vector(self): return list(self._vector)
    def dominant_state(self):
        if not self._vector: return None
        return max(set(self._vector), key=self._vector.count)

@dataclass
class Verifier:
    node_id: str = "VRF-001"
    def verify(self, receiver: Receiver) -> tuple:
        vector = receiver.calibration_vector
        if not vector: return False, "empty-vector"
        dominant = receiver.dominant_state()
        is_valid = (dominant == ByteState.SIGNAL)
        raw = bytes([int(s) for s in vector])
        fp  = hashlib.sha256(raw).hexdigest()[:16]
        return is_valid, fp

class CalibrationSession:
    def __init__(self, transmitter=None, receiver=None, verifier=None):
        self.tx  = transmitter or Transmitter()
        self.rx  = receiver    or Receiver()
        self.vrf = verifier    or Verifier()

    def run(self, payloads: list) -> dict:
        self.rx.receive(self.tx.connect())
        for p in payloads:
            self.rx.receive(self.tx.emit(p))
        valid, fp = self.vrf.verify(self.rx)
        self.rx.receive(self.tx.disconnect())
        return {"connected": valid, "fingerprint": fp,
                "dominant": self.rx.dominant_state(),
                "vector_len": len(self.rx.calibration_vector)}
