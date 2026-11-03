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
                        executable, args, cwd, env, data, timeout, output_limit
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

    async def _execute(self, executable, args, cwd, env, data, timeout, limit):
        loop = asyncio.get_running_loop()
        capture = _Capture(limit)
        spawn = asyncio.create_task(
            loop.subprocess_exec(
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
        )
        transport = None
        tasks = []
        status = "execution-failure"
        cancelled = False
        try:
            try:
                transport, _ = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Acquire the handle even if cancellation raced OS spawn.
                cancelled = True
                transport, _ = await spawn
                raise
            pipe = transport.get_pipe_transport(0)

            async def feed():
                try:
                    for index in range(0, len(data), 16384):
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
            cleanup_task = asyncio.create_task(
                self._cleanup(transport, capture, tasks)
            )
            while True:
                try:
                    cleanup_ok = await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    cancelled = True
            if not cleanup_ok:
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
        cleanup_ok = True
        if transport is not None:
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
                await asyncio.sleep(grace)
            transport.close()  # public API closes pipes even if an escaped child holds them
            try:
                await asyncio.wait_for(capture.closed.wait(), 0.3)
            except TimeoutError:
                cleanup_ok = False
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return cleanup_ok
