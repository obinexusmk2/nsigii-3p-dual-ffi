"""
NSIGII Windows Service
OBINexus / NSIGII Constitutional Computing Framework

Registers as a Windows SCM (Service Control Manager) service.
Manages the NSIGIIPeer lifecycle: start, stop, and background
reconnection to configured remote peers.

Usage (run as Administrator):
  python nsigii_service.py install    # register with SCM
  python nsigii_service.py start      # start the service
  python nsigii_service.py stop       # stop the service
  python nsigii_service.py remove     # unregister from SCM
  python nsigii_service.py debug      # run interactively (no SCM)

Config file:  nsigii_config.json  (next to this script)
Log file:     C:\\ProgramData\\NSIGII\\nsigii_service.log

Requires:     pip install pywin32
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time

# ---- pywin32 imports (Windows only) ----
try:
    import win32event
    import win32service
    import win32serviceutil
    import servicemanager
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False

# Ensure the directory containing this script is on sys.path so that
# nsigii_peer and mmuko_connect_calibration can be imported.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from nsigii_peer import NSIGIIPeer, PeerInfo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME        = "NSIGIIService"
SERVICE_DISPLAY     = "NSIGII Network Protocol Service"
SERVICE_DESCRIPTION = (
    "OBINexus NSIGII peer-to-peer network service with MMUKO calibration. "
    "Part of the NSIGII Constitutional Computing Framework."
)

CONFIG_PATH = os.path.join(_HERE, "nsigii_config.json")
LOG_DIR     = os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "NSIGII")
LOG_PATH    = os.path.join(LOG_DIR, "nsigii_service.log")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Also log to stdout when running in debug mode
    if sys.stdout and sys.stdout.isatty():
        logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    return logging.getLogger("nsigii.service")


def _load_config() -> dict:
    """Load nsigii_config.json, falling back to safe defaults."""
    defaults: dict = {
        "node_id":            socket.gethostname(),
        "listen_port":        9200,
        "remote_peers":       [],   # [{"host": "...", "port": 9200}]
        "reconnect_interval": 30,   # seconds between reconnect attempts
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                on_disk = json.load(fh)
            defaults.update(on_disk)
        except Exception as exc:
            print(f"[nsigii] Warning: could not parse {CONFIG_PATH}: {exc}", file=sys.stderr)
    return defaults


# ---------------------------------------------------------------------------
# Core service runner (shared by both SCM and debug mode)
# ---------------------------------------------------------------------------

class _NSIGIIRunner:
    """
    Encapsulates the peer start-up and reconnect logic.
    Used by both the SCM service class and the debug runner.
    """

    def __init__(self, config: dict):
        self.config = config
        self.peer:   NSIGIIPeer | None = None
        self.logger = logging.getLogger("nsigii.runner")
        self._reconnect_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()

    def start(self) -> None:
        cfg            = self.config
        node_id        = cfg["node_id"]
        listen_port    = int(cfg["listen_port"])
        remote_peers   = cfg.get("remote_peers", [])
        reconnect_secs = int(cfg.get("reconnect_interval", 30))

        self.logger.info(
            "Starting NSIGII runner — node_id=%s port=%d remote_peers=%s",
            node_id, listen_port, remote_peers,
        )

        self.peer = NSIGIIPeer(node_id=node_id, listen_port=listen_port)
        self.peer.on_peer_connected    = self._on_connected
        self.peer.on_peer_disconnected = self._on_disconnected
        self.peer.on_data              = self._on_data
        self.peer.start()

        if remote_peers:
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                args=(remote_peers, reconnect_secs),
                daemon=True,
                name="nsigii-reconnect",
            )
            self._reconnect_thread.start()

    def stop(self) -> None:
        self._stop_flag.set()
        if self.peer:
            self.peer.stop()
        self.logger.info("NSIGII runner stopped")

    # -- Callbacks -----------------------------------------------------------

    def _on_connected(self, peer: PeerInfo) -> None:
        self.logger.info(
            "Peer CONNECTED: node_id=%s host=%s port=%d fingerprint=%s",
            peer.node_id, peer.host, peer.port, peer.fingerprint,
        )

    def _on_disconnected(self, peer: PeerInfo) -> None:
        self.logger.info(
            "Peer DISCONNECTED: node_id=%s host=%s port=%d",
            peer.node_id, peer.host, peer.port,
        )

    def _on_data(self, peer: PeerInfo, data: bytes) -> None:
        self.logger.info(
            "DATA from %s (%d bytes): %r…",
            peer.node_id, len(data), data[:64],
        )

    # -- Reconnect loop ------------------------------------------------------

    def _reconnect_loop(self, remote_peers: list[dict], interval: int) -> None:
        """Periodically attempt to connect to each configured remote peer."""
        while not self._stop_flag.wait(interval):
            for rp in remote_peers:
                host = rp.get("host", "").strip()
                port = int(rp.get("port", 9200))
                if not host:
                    continue
                already_connected = any(
                    p.host == host and p.port == port
                    for p in (self.peer.connected_peers if self.peer else [])
                )
                if not already_connected:
                    self.logger.info("Reconnect attempt → %s:%d", host, port)
                    if self.peer:
                        self.peer.connect_to(host, port)


# ---------------------------------------------------------------------------
# Windows SCM Service class
# ---------------------------------------------------------------------------

if _HAS_WIN32:
    class NSIGIIWindowsService(win32serviceutil.ServiceFramework):
        _svc_name_         = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_  = SERVICE_DESCRIPTION

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._runner: _NSIGIIRunner | None = None

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop_event)
            if self._runner:
                self._runner.stop()

        def SvcDoRun(self) -> None:
            logger = _setup_logging()
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            logger.info("NSIGIIWindowsService SvcDoRun started")

            config       = _load_config()
            self._runner = _NSIGIIRunner(config)
            self._runner.start()

            # Block until the stop event is signalled
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
            logger.info("NSIGIIWindowsService SvcDoRun exiting")


# ---------------------------------------------------------------------------
# Debug / standalone runner (no SCM)
# ---------------------------------------------------------------------------

def _run_debug() -> None:
    """Run the service interactively for development/testing."""
    logger = _setup_logging()
    logger.info("Running NSIGII in debug (non-service) mode — Ctrl-C to stop")
    print(f"[nsigii] Debug mode.  Logs → {LOG_PATH}")

    config = _load_config()
    runner = _NSIGIIRunner(config)
    runner.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[nsigii] Stopping…")
        runner.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _HAS_WIN32:
        # pywin32 not installed — fall back to debug mode
        print("[nsigii] pywin32 not found.  Running in standalone debug mode.")
        _run_debug()
        sys.exit(0)

    args = sys.argv[1:]

    if not args or args[0].lower() == "debug":
        _run_debug()
    elif len(sys.argv) == 1:
        # Called by SCM with no arguments — start the dispatcher
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(NSIGIIWindowsService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(NSIGIIWindowsService)
