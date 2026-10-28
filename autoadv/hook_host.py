"""Parent side of isolated native Hook Host lifecycle and event protocol."""
from __future__ import annotations
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from collections.abc import Callable
from dataclasses import asdict
from .hook_protocol import HookEvent,ThreadSource
from .crash_report import record_exception, record_failure


def parse_host_event(packet: dict) -> HookEvent:
    if not isinstance(packet, dict):
        raise ValueError('invalid native Host packet')
    if packet.get('kind') == 'fatal':
        if set(packet) != {'kind', 'reason'} or not isinstance(packet['reason'], str) or not packet['reason']:
            raise ValueError('invalid native Host fatal packet')
        raise RuntimeError(packet['reason'])
    expected = {
        'kind', 'host_generation', 'connection_generation', 'sequence', 'received_ns',
        'process_id', 'source', 'text', 'detail',
    }
    if set(packet) != expected:
        raise ValueError('invalid native Host event packet')
    source = packet['source']
    if source is not None:
        source = ThreadSource(**source)
    return HookEvent(
        kind=packet['kind'], host_generation=packet['host_generation'],
        connection_generation=packet['connection_generation'], sequence=packet['sequence'],
        received_ns=packet['received_ns'], process_id=packet['process_id'],
        source=source, text=packet['text'], detail=packet['detail'])

class HookHost:
    MAX_PENDING_EVENTS = 4096
    MAX_BATCH_EVENTS = 256
    BATCH_COALESCE_SECONDS = 0.005

    def __init__(self,dll_path:Path,on_events:Callable[[tuple[HookEvent, ...]],None],on_closed:Callable[[str],None],
                 on_error:Callable[[str],None] | None = None):
        self.dll_path=Path(dll_path)
        self.on_events=on_events
        self.on_closed=on_closed
        self.on_error=on_error or (lambda _:None)
        self.child=None
        self.thread=None
        self.dispatch_thread=None
        self.error_thread=None
        self._send_lock=threading.Lock()
        self._events: queue.Queue[HookEvent | tuple[None, str]] = queue.Queue(
            maxsize=self.MAX_PENDING_EVENTS)

    def start(self):
        if self.child is not None:raise RuntimeError('Host already running')
        if not self.dll_path.is_file():raise FileNotFoundError(self.dll_path)
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        self.child=subprocess.Popen([sys.executable,'-m','autoadv.hook_host_worker','--dll',str(self.dll_path)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8',bufsize=1,creationflags=flags)
        self.dispatch_thread=threading.Thread(
            target=self._dispatch_events,name='autoadv-native-host-dispatch',daemon=True)
        self.dispatch_thread.start()
        self.thread=threading.Thread(target=self._reader,name='autoadv-native-host-reader',daemon=True)
        self.thread.start()
        self.error_thread=threading.Thread(target=self._read_errors,name='autoadv-native-host-errors',daemon=True)
        self.error_thread.start()

    def _read_errors(self):
        # ctypes callback exceptions go to stderr, not to the JSON event pipe.
        # Discarding stderr made an alive Host with zero visible HOOKs impossible
        # to diagnose. Keep each channel separate to preserve IPC framing.
        child = self.child
        for line in child.stderr:
            if line.strip():
                self.on_error(line.rstrip())

    def _reader(self):
        reason='native_host_exit'
        child = self.child
        try:
            for line in child.stdout:
                event=parse_host_event(json.loads(line))
                # Bounded queue is deliberate backpressure.  We preserve every
                # ordered Host fact instead of converting a burst into thousands
                # of GUI callbacks or allowing unbounded process memory growth.
                self._events.put(event)
        except Exception as exc:
            reason=f'native_host_ipc_failure: {type(exc).__name__}: {exc}'
            record_exception('native Hook Host IPC reader failure', exc)
        finally:
            exit_code = child.poll() if child is not None else None
            if exit_code not in (None, 0):
                record_failure(f'native Hook Host exited with code {exit_code}')
            self._events.put((None, reason))

    def _dispatch_events(self):
        """Preserve Host order while crossing into Application in bounded batches."""
        while True:
            first = self._events.get()
            if isinstance(first, tuple):
                self.on_closed(first[1])
                return
            batch = [first]
            closed_reason = None
            deadline = time.monotonic() + self.BATCH_COALESCE_SECONDS
            while len(batch) < self.MAX_BATCH_EVENTS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._events.get(timeout=remaining)
                except queue.Empty:
                    break
                if isinstance(item, tuple):
                    closed_reason = item[1]
                    break
                batch.append(item)
            try:
                self.on_events(tuple(batch))
            except Exception as exc:
                record_exception('native Hook Host batch callback failure', exc)
            if closed_reason is not None:
                self.on_closed(closed_reason)
                return

    def command(self,action:str,**kwargs):
        if self.child is None or self.child.poll() is not None:raise RuntimeError('native Host not running')
        with self._send_lock:
            self.child.stdin.write(json.dumps({'action':action,**kwargs},ensure_ascii=False)+'\n')
            self.child.stdin.flush()

    def close(self):
        if self.child is None:return
        try:
            if self.child.poll() is None:
                self.command('stop')
                self.child.wait(timeout=3)
        except (BrokenPipeError,RuntimeError,subprocess.TimeoutExpired):
            # Kill only the subprocess we created, not an external game or Luna.
            if self.child.poll() is None:self.child.kill()
        finally:
            self.child.wait(timeout=3)
            self.child=None
            for thread in (self.thread, self.dispatch_thread, self.error_thread):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=2)
            self.thread = self.dispatch_thread = self.error_thread = None
