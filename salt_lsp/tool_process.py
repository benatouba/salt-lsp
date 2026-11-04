"""Bounded Linux process-group runner; not a sandbox for trusted programs."""

import asyncio
from dataclasses import dataclass
import os
import signal
import sys


@dataclass(frozen=True)
class ProcessResult:
    """Raw output is provider-private: never forward it to logs/status messages."""

    status: str
    stdout: bytes = b""
    stderr: bytes = b""
    returncode: int | None = None
    message: str = ""


async def _hold(budget, until=None):
    """Spend cleanup time that cancellation cannot shorten.

    Loop teardown cancels every task once, so cleanup must neither run in a
    separately cancellable task nor let a CancelledError abandon a process.
    Returns whether `until` was set within the budget.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while not (until and until.is_set()):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.sleep(min(remaining, 0.01))
        except asyncio.CancelledError:
            pass
    return bool(until and until.is_set())


class _Capture(asyncio.SubprocessProtocol):
    def __init__(self, limit):
        self.limit = limit
        self.output = {1: bytearray(), 2: bytearray()}
        self.overflow = asyncio.Event()
        self.exited = asyncio.Event()
        self.closed = asyncio.Event()
        self.io_error = asyncio.Event()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def pipe_data_received(self, fd, data):
        if fd in self.output:
            output = self.output[fd]
            remaining = self.limit - len(output)
            output.extend(data[:remaining])
            if len(data) > remaining:
                self.overflow.set()

    def process_exited(self):
        self.exited.set()

    def connection_lost(self, exc):
        if exc is not None:
            self.io_error.set()
        self.closed.set()

    def pipe_connection_lost(self, fd, exc):
        if exc is not None and not isinstance(
            exc, (BrokenPipeError, ConnectionResetError)
        ):
            self.io_error.set()


class ProcessRunner:
    """Two active jobs, eight waiting; slots remain owned through cleanup."""

    def __init__(self):
        self.slots = asyncio.Semaphore(2)
        self.pending = 0
        self.owners: set[asyncio.Task] = set()

    async def run(
        self,
        executable,
        args,
        *,
        cwd,
        env,
        data=b"",
        valid=lambda: True,
        timeout=5.0,
        output_limit=262144,
    ):
        """Exec argv only; finite input/output/time. Recheck permission after queue."""
        if sys.platform != "linux":
            return ProcessResult("unsupported-platform")
        if len(data) > 4 * 1024 * 1024:
            return ProcessResult("input-limit")
        if self.pending >= 10:
            return ProcessResult("busy")
        timeout = min(max(timeout, 0.01), 30.0)
        output_limit = min(max(output_limit, 1), 1024 * 1024)
        self.pending += 1
        try:
            async with self.slots:
                if not valid():
                    return ProcessResult("stale")
                owner = asyncio.create_task(
                    self._execute(
                        executable,
                        args,
                        cwd,
                        env,
                        data,
                        timeout,
                        output_limit,
                        valid,
                    )
                )
                self.owners.add(owner)
                try:
                    return await asyncio.shield(owner)
                except asyncio.CancelledError:
                    owner.cancel()
                    # Repeated cancellation must not detach the cleanup owner.
                    while not owner.done():
                        try:
                            await asyncio.shield(owner)
                        except asyncio.CancelledError:
                            continue
                    raise
                finally:
                    self.owners.discard(owner)
        finally:
            self.pending -= 1

    async def _execute(
        self, executable, args, cwd, env, data, timeout, limit, valid
    ):
        loop = asyncio.get_running_loop()
        capture = _Capture(limit)

        async def spawn_owned():
            if not valid():
                return None, capture
            return await loop.subprocess_exec(
                lambda: capture,
                str(executable),
                *args,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )

        spawn = asyncio.create_task(spawn_owned())
        transport = None
        tasks = []
        status = "execution-failure"
        cancelled = False
        try:
            # Acquire the handle even if cancellation, repeatedly, races OS spawn.
            while not spawn.done():
                try:
                    await asyncio.wait({spawn})
                except asyncio.CancelledError:
                    cancelled = True
            transport, _ = spawn.result()
            if cancelled:
                raise asyncio.CancelledError
            if transport is None:
                return ProcessResult("stale")
            pipe = transport.get_pipe_transport(0)

            async def feed():
                try:
                    for index in range(0, len(data), 16384):
                        if pipe.is_closing():
                            break
                        while pipe.get_write_buffer_size() > 32768:
                            await asyncio.sleep(0.005)
                        pipe.write(data[index : index + 16384])
                        await asyncio.sleep(0)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except OSError:
                    capture.io_error.set()
                finally:
                    pipe.close()

            tasks = [
                asyncio.create_task(feed()),
                asyncio.create_task(capture.exited.wait()),
                asyncio.create_task(capture.overflow.wait()),
                asyncio.create_task(capture.io_error.wait()),
            ]
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait(
                    tasks[1:], return_when=asyncio.FIRST_COMPLETED
                )
                del done
                status = (
                    "execution-failure"
                    if capture.io_error.is_set()
                    else (
                        "output-limit"
                        if capture.overflow.is_set()
                        else (
                            "clean"
                            if transport.get_returncode() == 0
                            else "tool-failure"
                        )
                    )
                )
        except TimeoutError:
            status = "timeout"
        except asyncio.CancelledError:
            cancelled = True
        except (OSError, ValueError):
            status = "spawn-failure"
        finally:
            if not await self._cleanup(transport, capture, tasks):
                status = "cleanup-failure"
        if cancelled:
            raise asyncio.CancelledError
        if capture.overflow.is_set() and status != "cleanup-failure":
            status = "output-limit"
        elif capture.io_error.is_set() and status == "clean":
            status = "execution-failure"
        return ProcessResult(
            status,
            bytes(capture.output[1]),
            bytes(capture.output[2]),
            transport.get_returncode() if transport else None,
        )

    @staticmethod
    async def _cleanup(transport, capture, tasks):
        """Stop the group, then wait for EOF so no written output is dropped.

        False means a process escaped the group and kept its pipes open, or the
        transport would not close: the captured output may be incomplete.
        """
        for task in tasks:
            task.cancel()
        if transport is None:
            return True
        cleanup_ok = True
        pid = transport.get_pid()
        # Always clean the group, even when its leader already exited.
        for sig, grace in ((signal.SIGTERM, 0.1), (signal.SIGKILL, 0.1)):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                cleanup_ok = False
                break
            await _hold(grace)
        if not await _hold(0.3, capture.closed):
            cleanup_ok = False
        # Public API: closes pipes even when an escaped child still holds them.
        transport.close()
        if not await _hold(0.1, capture.closed):
            cleanup_ok = False
        return cleanup_ok
