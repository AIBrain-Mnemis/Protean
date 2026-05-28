"""BridgeSupervisor — spawn and supervise the Electron bridge subprocess.

Lifecycle:
  1. spawn `electron electron-bridge/dist/main.js`
  2. read one line of stdout = bootstrap JSON {"protocol", "port", "token", "pid"}
  3. yield (port, token) so a RealtimeBridgeClient can connect
  4. on bridge exit, retry with 1s/4s/16s backoff (3 attempts), then FATAL
  5. closing our end of bridge.stdin signals the bridge to die

The Electron binary is resolved by running `node scripts/print-electron-path.cjs`
once and caching the path. Override via PROTEAN_ELECTRON_BIN.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Backoff schedule for bridge respawn.
_BACKOFF_SECS: tuple[float, ...] = (1.0, 4.0, 16.0)
_BOOTSTRAP_TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class BridgeBootstrap:
    """First line of bridge stdout."""

    protocol: int
    port: int
    token: str
    pid: int


class BridgeError(Exception):
    """Bridge could not be started or exited unexpectedly past the retry budget."""


class BridgeSupervisor:
    """Owns the bridge subprocess. One instance per Python daemon.

    Not thread-safe; expected to live in the daemon's main asyncio loop.
    """

    def __init__(
        self,
        bridge_dir: Path | None = None,
        electron_bin: str | None = None,
    ) -> None:
        self._bridge_dir = bridge_dir or _default_bridge_dir()
        self._electron_bin_override = electron_bin
        self._electron_bin: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._bootstrap: BridgeBootstrap | None = None
        self._stopped = False
        self._stderr_file: Any = None  # opened FD for bridge stderr
        self._stderr_log_path: Path | None = None

    @property
    def stderr_log_path(self) -> Path | None:
        """Path bridge stderr is being written to; None until start()."""
        return self._stderr_log_path

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> BridgeBootstrap:
        """Spawn the bridge with retries. Returns the bootstrap once successful."""
        if self._proc is not None:
            raise RuntimeError("BridgeSupervisor already started")

        last_err: Exception | None = None
        for attempt, delay in enumerate((0.0, *_BACKOFF_SECS)):
            if delay:
                log.warning("Bridge restart attempt %d after %.1fs", attempt, delay)
                await asyncio.sleep(delay)
            try:
                bootstrap = await self._spawn_once()
                self._bootstrap = bootstrap
                log.info(
                    "Bridge ready: pid=%d port=%d protocol=%d",
                    bootstrap.pid, bootstrap.port, bootstrap.protocol,
                )
                return bootstrap
            except Exception as e:
                last_err = e
                log.warning("Bridge spawn failed: %s", e)
                await self._teardown_subprocess()

        raise BridgeError(
            f"Bridge failed to start after {len(_BACKOFF_SECS) + 1} attempts: {last_err}"
        )

    async def stop(self) -> None:
        """Close stdin to signal bridge shutdown; wait briefly for exit."""
        self._stopped = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            log.debug("Closing bridge stdin failed", exc_info=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("Bridge did not exit within 2s; killing")
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        finally:
            await self._teardown_subprocess()

    @property
    def bootstrap(self) -> BridgeBootstrap | None:
        return self._bootstrap

    # ── Internals ────────────────────────────────────────────────────────

    async def _spawn_once(self) -> BridgeBootstrap:
        entry = self._bridge_dir / "dist" / "main.js"
        if not entry.exists():
            raise BridgeError(
                f"Bridge entry not found: {entry}. Run `npm install && npm run build` "
                f"in {self._bridge_dir}."
            )

        if self._electron_bin is None:
            self._electron_bin = await self._discover_electron_bin()

        env = os.environ.copy()
        # Suppress Node's experimental-feature warnings on stderr.
        env.setdefault("NODE_NO_WARNINGS", "1")
        # Electron tries to talk to a system D-Bus on Linux; harmless to silence.
        env.setdefault("ELECTRON_NO_ATTACH_CONSOLE", "1")

        # Open the bridge stderr log file. We write directly to a file
        # rather than PIPE+drain (drain task dies with the asyncio.run() loop)
        # or stderr=None (Electron on Windows with ELECTRON_NO_ATTACH_CONSOLE
        # detaches from the parent's console and the inherited stderr is
        # discarded). A real file always works and can be `tail -f`'d.
        self._stderr_log_path = self._bridge_dir / "bridge.log"
        # Truncate per spawn so the file reflects only the current run.
        self._stderr_file = open(self._stderr_log_path, "wb", buffering=0)
        log.info("Bridge stderr -> %s", self._stderr_log_path)

        self._proc = await asyncio.create_subprocess_exec(
            self._electron_bin,
            str(entry),
            cwd=str(self._bridge_dir),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self._stderr_file,
            env=env,
        )

        bootstrap = await asyncio.wait_for(
            self._read_bootstrap(), timeout=_BOOTSTRAP_TIMEOUT_SEC,
        )
        return bootstrap

    async def _discover_electron_bin(self) -> str:
        """Resolve the Electron binary path.

        Order:
          1. constructor override
          2. PROTEAN_ELECTRON_BIN env var
          3. `node scripts/print-electron-path.cjs` (uses electron-bridge's
             local `electron` npm package)
        """
        if self._electron_bin_override:
            return self._electron_bin_override
        env_override = os.environ.get("PROTEAN_ELECTRON_BIN")
        if env_override:
            return env_override

        node_bin = _resolve_node_bin()
        script = self._bridge_dir / "scripts" / "print-electron-path.cjs"
        if not script.exists():
            raise BridgeError(
                f"Electron path resolver script missing: {script}. "
                "The electron-bridge subproject layout is broken."
            )

        proc = await asyncio.create_subprocess_exec(
            node_bin,
            str(script),
            cwd=str(self._bridge_dir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise BridgeError("Electron path resolver timed out") from e

        if proc.returncode != 0:
            raise BridgeError(
                f"Electron path resolver failed (rc={proc.returncode}): "
                f"{err.decode('utf-8', errors='replace').strip()}"
            )

        path = out.decode("utf-8").strip()
        if not path:
            raise BridgeError("Electron path resolver produced empty output")
        if not Path(path).exists():
            raise BridgeError(
                f"Electron binary not found at {path!r}. "
                f"Run `npm install` in {self._bridge_dir}."
            )
        return path

    async def _read_bootstrap(self) -> BridgeBootstrap:
        assert self._proc and self._proc.stdout
        # Some runtimes (notably Electron on Windows) emit blank lines or other
        # startup chatter on stdout before our bootstrap. Skip empty lines and
        # any non-JSON lines until we find the bootstrap object. Cap at 8 lines
        # so we fail fast on a real bug.
        line: bytes = b""
        for _ in range(8):
            line = await self._proc.stdout.readline()
            if not line:
                rc = self._proc.returncode
                raise BridgeError(
                    f"Bridge exited before bootstrap (rc={rc})"
                )
            stripped = line.strip()
            if not stripped:
                continue  # blank chatter
            if not stripped.startswith(b"{"):
                # Pre-bootstrap log line (e.g. Electron debug). Log and skip.
                log.debug("Skipping pre-bootstrap stdout: %r", stripped[:120])
                continue
            break
        else:
            raise BridgeError("Bootstrap not seen after 8 lines of stdout")

        try:
            obj = json.loads(line.decode("utf-8").strip())
        except Exception as e:
            raise BridgeError(f"Bad bootstrap line: {line!r}: {e}") from e

        for k in ("protocol", "port", "token", "pid"):
            if k not in obj:
                raise BridgeError(f"Bootstrap missing field {k!r}: {obj!r}")

        if not isinstance(obj["protocol"], int) or not isinstance(obj["port"], int):
            raise BridgeError(f"Bootstrap field types wrong: {obj!r}")
        if not isinstance(obj["token"], str) or not obj["token"]:
            raise BridgeError("Bootstrap token must be non-empty string")

        return BridgeBootstrap(
            protocol=obj["protocol"],
            port=obj["port"],
            token=obj["token"],
            pid=obj["pid"],
        )

    async def _teardown_subprocess(self) -> None:
        proc = self._proc
        self._proc = None
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

        # Close the stderr log file (file remains on disk for inspection).
        if self._stderr_file is not None:
            try:
                self._stderr_file.close()
            except Exception:
                pass
            self._stderr_file = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _default_bridge_dir() -> Path:
    """Resolve to <repo_root>/electron-bridge.

    Walks up from this file (protean/channels/bridge_supervisor.py).
    """
    here = Path(__file__).resolve()
    # protean/channels/bridge_supervisor.py -> protean/channels -> protean -> repo root
    return here.parent.parent.parent / "electron-bridge"


def _resolve_node_bin() -> str:
    """Locate `node` on PATH. Allow override via PROTEAN_NODE_BIN.

    Only used to run scripts/print-electron-path.cjs — the bridge itself runs
    under the Electron binary, not Node.
    """
    override = os.environ.get("PROTEAN_NODE_BIN")
    if override:
        return override
    found = shutil.which("node")
    if not found:
        raise BridgeError(
            "`node` not found on PATH (needed to discover Electron binary). "
            "Set PROTEAN_NODE_BIN or install Node 22+, "
            "or set PROTEAN_ELECTRON_BIN to bypass discovery."
        )
    return found
