"""The thread that owns the scope between captures.

Two modes drive the device with block captures — the scope's triggered sweeps
(:mod:`picoscope_4824a.sweep`) and DAQ runs (:mod:`picoscope_4824a.daq`) — and
both need the same three things from the thread they run on. Each one strands
the driver if it is wrong, which is why they live here once rather than twice:

* a capture that can be **released from another thread**, because a capture
  waiting on a trigger may wait for ever;
* a way to apply a device change **between** captures, since the device object
  is not thread-safe and the change must not queue behind that wait;
* a **stop** that works from either state, so the handle is always released.

Subclasses write their own loop body over :meth:`AcquisitionLoop.iterations`,
which handles the ordering those three need between them.
"""

from __future__ import annotations

import threading
import time
from time import perf_counter
from typing import Any, Callable, Optional


class _PendingCall:
    """Work handed to the loop thread, with its result handed back."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        self.fn = fn
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None

    def run(self) -> None:
        try:
            self.result = self.fn()
        except BaseException as exc:            # returned to the caller
            self.error = exc
        finally:
            self.done.set()


class AcquisitionLoop:
    """A stoppable background loop that owns the device while it runs."""

    #: Thread name, for anyone reading a stack dump.
    thread_name = "ps4000a-loop"

    def __init__(self, scope) -> None:
        self._scope = scope
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        #: Set to release a capture that is waiting — to stop, to re-arm with
        #: new settings, or to let a queued call through.
        self._interrupt = threading.Event()
        self._calls: list = []
        self._accepting = False
        self._stopped_reason: Optional[str] = None

    # --- state ------------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return (thread is not None and thread.is_alive()
                and not self._stopping.is_set())

    # --- lifecycle --------------------------------------------------------

    def _start_thread(self) -> None:
        """Start the loop. Callers hold the lock and have checked they may."""
        self._stopping.clear()
        self._interrupt.clear()
        self._accepting = True
        self._stopped_reason = None
        self._thread = threading.Thread(target=self._thread_main,
                                        name=self.thread_name, daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0, reason: str = "stopped") -> bool:
        """Stop the loop. True if the thread exited within *timeout_s*."""
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return True
            if not self._stopping.is_set():
                self._stopped_reason = reason
            self._stopping.set()
            self._interrupt.set()
        if thread is threading.current_thread():
            return True
        thread.join(timeout=timeout_s)
        return not thread.is_alive()

    # --- work from other threads -----------------------------------------

    def run_on_loop(self, fn: Callable[[], Any], timeout_s: float = 10.0) -> Any:
        """Run *fn* where it is safe to touch the device, and return its result.

        While the loop runs that is this thread, between captures — a waiting
        capture is released so the call does not queue behind a trigger that
        may never come. Otherwise *fn* runs in the caller. Exceptions propagate.
        """
        with self._lock:
            queued = (self._accepting
                      and self._thread is not None
                      and self._thread is not threading.current_thread())
            if queued:
                call = _PendingCall(fn)
                self._calls.append(call)
                self._interrupt.set()
        if not queued:
            return fn()
        if not call.done.wait(timeout_s):
            raise TimeoutError(f"the scope did not reach a safe point within "
                               f"{timeout_s:g} s to apply the change")
        if call.error is not None:
            raise call.error
        return call.result

    def _drain_calls(self) -> None:
        while True:
            with self._lock:
                if not self._calls:
                    return
                call = self._calls.pop(0)
            call.run()

    # --- the loop ---------------------------------------------------------

    def iterations(self):
        """Yield once per pass, at the point where the device is free.

        Handles the ordering the three guarantees need: queued work runs first,
        a stop ends the loop, and the interrupt is only cleared once there is
        nothing waiting — clearing it with a call still queued would swallow
        that call's wakeup, and the next capture could then wait for ever with
        its caller stuck behind it.
        """
        while True:
            self._drain_calls()
            with self._lock:
                if self._stopping.is_set():
                    return
                if self._calls:            # arrived after the drain above
                    continue
                self._interrupt.clear()
            yield

    def _pause(self, seconds: float) -> None:
        """Sleep up to *seconds*, cut short by an interrupt.

        time.sleep in short steps rather than Event.wait: on Windows a timed
        Event.wait rounds up to the ~15 ms scheduler tick.
        """
        end = perf_counter() + seconds
        while not self._interrupt.is_set():
            left = end - perf_counter()
            if left <= 0:
                return
            time.sleep(min(left, 0.005))

    def _work(self) -> None:
        """The loop body. Subclasses implement it over :meth:`iterations`."""
        raise NotImplementedError

    def _on_exit(self) -> None:
        """Bookkeeping as the thread leaves, under the lock. Optional."""

    def _thread_main(self) -> None:
        try:
            self._work()
        except BaseException as exc:
            # Record it where the surfaces can see it, then let it reach the
            # thread's excepthook: a driver fault belongs in the server log
            # with its traceback, not swallowed into a state field.
            with self._lock:
                self._stopped_reason = f"acquisition loop failed: {exc}"
            raise
        finally:
            with self._lock:
                self._accepting = False
                pending, self._calls = self._calls, []
                try:
                    self._on_exit()
                except Exception:
                    pass
            # The device is idle now, so anything still queued runs here
            # rather than leaving its caller to time out.
            for call in pending:
                call.run()
