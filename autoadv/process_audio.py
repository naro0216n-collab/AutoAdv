"""Bounded binary bridge to Windows WASAPI *process-specific* loopback.

Transport uses 48 kHz float32 stereo. Absence of PCM packets is normal for
an idle game; heartbeat only confirms capture process is still responsive,
not that a given line has no voice. A dead helper fails closed.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from typing import BinaryIO, Callable
import os
import struct
import subprocess
import sys
import time
from .audio_buffer import PCMChunk, AudioHistory

HEADER = struct.Struct('<IqI')
MAX_PCM_PACKET = 48000 * 2 * 4  # at most 1 second per packet
# Called in the GUI's existing background loader, never on the UI thread.
CAPTURE_STARTUP_TIMEOUT_SECONDS = 12


@dataclass(frozen=True)
class CaptureHealth:
    at_ms: int
    healthy: bool
    reason: str
    pcm_frames: int = 0  # capture transport evidence, not foreground speech


def read_packet(stream: BinaryIO) -> tuple[int, PCMChunk | None]:
    raw = stream.read(HEADER.size)
    if len(raw) != HEADER.size:
        raise EOFError('WASAPI helper stream closed')
    count, timestamp, kind = HEADER.unpack(raw)
    if count > MAX_PCM_PACKET or count % 8 or timestamp < 0:
        raise ValueError('invalid capture frame')
    if kind == 0 and count == 0:
        return timestamp, None
    if kind != 1 or count == 0:
        raise ValueError('unexpected capture format')
    audio = stream.read(count)
    if len(audio) != count:
        raise EOFError('truncated process audio frame')
    return timestamp, PCMChunk(timestamp, audio, 48000, 2, 4)


class ProcessCapture:
    def __init__(self, pid: int, helper_dll: Path, history: AudioHistory,
                 on_chunk: Callable[[PCMChunk], None],
                 on_health: Callable[[CaptureHealth], None],
                 clock: Callable[[], int] = lambda: time.monotonic_ns() // 1_000_000):
        if pid <= 0:
            raise ValueError('target PID required')
        self.pid, self.helper_dll, self.history = pid, helper_dll, history
        self.on_chunk, self.on_health, self.clock = on_chunk, on_health, clock
        self.stop_requested = Event()
        self.process: subprocess.Popen | None = None
        self.worker: Thread | None = None
        self._lock = Lock()
        self._startup_ack = Event()
        self._startup_failure: str | None = None
        self._helper_clock_offset_ms: int | None = None

    def _local_timestamp(self, helper_at_ms: int) -> int:
        """Map the helper's monotonic clock into the controller clock domain.

        PCM packets carry the captured-frame end derived from WASAPI's QPC
        packet position; heartbeats use that same helper clock domain.  The
        Python reader may consume them later, so receipt time is not capture
        time.  A fixed offset preserves
        the helper's packet-to-packet chronology while keeping every timestamp in
        the same monotonic domain as Controller deadlines and input boundaries.
        """
        helper_at_ms = int(helper_at_ms)
        if self._helper_clock_offset_ms is None:
            self._helper_clock_offset_ms = int(self.clock()) - helper_at_ms
        return helper_at_ms + self._helper_clock_offset_ms

    def start(self) -> None:
        if sys.platform != 'win32' or not self.helper_dll.is_file():
            raise OSError('Windows and built process-loopback helper required')
        if self.process is not None:
            raise RuntimeError('capture already started')
        self.history.start(self.pid)
        self.process = subprocess.Popen(
            ['dotnet', str(self.helper_dll), '--pid', str(self.pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.worker = Thread(target=self._run, daemon=True, name='autoadv-wasapi')
        self.worker.start()
        # Popen only acknowledges process creation, not WASAPI activation or
        # recording readiness. The helper emits its first heartbeat only after
        # StartRecording; a dead/uninitialized bridge must not be marked ready.
        if not self._startup_ack.wait(CAPTURE_STARTUP_TIMEOUT_SECONDS):
            self.close()
            raise TimeoutError('Process Loopback helper did not acknowledge capture readiness')
        if self._startup_failure:
            self.close()
            raise OSError('Process Loopback helper failed to initialize: ' + self._startup_failure)

    def _run(self) -> None:
        try:
            assert self.process and self.process.stdout
            while not self.stop_requested.is_set():
                helper_at, chunk = read_packet(self.process.stdout)
                now = self._local_timestamp(helper_at)
                if chunk is not None:
                    local_chunk = PCMChunk(now, chunk.frames, chunk.sample_rate,
                                           chunk.channels, chunk.sample_width_bytes)
                    if not self.history.push(local_chunk, target_pid=self.pid):
                        raise ValueError(self.history.fail_reason or 'PCM timeline rejected packet')
                    self.on_chunk(local_chunk)
                self.on_health(CaptureHealth(now, True, 'helper_alive',
                                             len(chunk.frames) // 8 if chunk is not None else 0))
                self._startup_ack.set()
        except (EOFError, OSError, ValueError) as exc:
            self.history.reset(str(exc))
            if not self._startup_ack.is_set():
                self._startup_failure = str(exc)
                self._startup_ack.set()
            if not self.stop_requested.is_set():
                self.on_health(CaptureHealth(self.clock(), False, str(exc)))
        finally:
            with self._lock:
                process = self.process
                if process and process.poll() is None:
                    process.terminate()

    def close(self) -> None:
        self.stop_requested.set()
        with self._lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
        if self.worker and self.worker is not __import__('threading').current_thread():
            self.worker.join(timeout=2)
        self.history.reset('stopped')
