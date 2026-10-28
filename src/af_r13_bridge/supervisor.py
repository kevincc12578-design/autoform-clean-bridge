"""Bounded, single-writer automation helpers, independently implemented.

Only privately contained worker process trees are terminated. AFFormingUI is
never assigned to a worker Job Object and is never killed. Windows workers are
created suspended, assigned to a kill-on-close job, and only then resumed; this
also contains the extra interpreter spawned by a Windows venv redirector.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

import psutil

from .evidence import Evidence


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


class WindowsWorkerJob:
    """Private, non-inheritable Job Object containing only our suspended Popen.

    Child processes inherit membership, not the job handle. No breakaway flag
    is enabled. No existing application process or arbitrary PID is admitted.
    """

    creation_flags = 0x08000000 | 0x00000004  # CREATE_NO_WINDOW | CREATE_SUSPENDED

    def __init__(self, evidence: Evidence, call_id: str):
        from ctypes import wintypes as w
        self.evidence, self.call_id = evidence, call_id
        self.closed = False
        self.process = None
        self.members = {}
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, w.LPVOID]
        k.QueryInformationJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k.OpenThread.restype = w.HANDLE
        k.ResumeThread.argtypes = [w.HANDLE]
        k.ResumeThread.restype = w.DWORD
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                        ("active_process_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                        ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            k.CloseHandle(self.handle)
            self.handle = None
            self.closed = True
            raise error
        self.evidence.append("worker_job_created", call_id=call_id, kill_on_close=True,
                             breakaway_allowed=False, created_suspended=True)

    def assign_and_resume(self, process: subprocess.Popen) -> None:
        self.process = process
        # CPython's Windows Popen owns this exact process HANDLE. No PID-based
        # process lookup is used to choose a process for job admission.
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.refresh()
        threads = psutil.Process(process.pid).threads()
        if len(threads) != 1:
            raise WorkerError("worker_start_failed", "Suspended worker must have exactly one initial thread")
        thread = self.kernel.OpenThread(0x0002, False, threads[0].id)  # THREAD_SUSPEND_RESUME
        if not thread:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            previous = self.kernel.ResumeThread(thread)
            if previous != 1:
                raise WorkerError("worker_start_failed", f"Unexpected worker suspend count {previous}")
        finally:
            self.kernel.CloseHandle(thread)
        self.evidence.append("worker_job_resumed", call_id=self.call_id, root_pid=process.pid,
                             initial_thread_id=threads[0].id)

    def active_pids(self) -> list[int]:
        if self.closed:
            return []
        from ctypes import wintypes as w
        capacity = 16
        while capacity <= 65536:
            class ProcessIds(ctypes.Structure):
                _fields_ = [("assigned", w.DWORD), ("count", w.DWORD),
                            ("pids", ctypes.c_size_t * capacity)]
            listing = ProcessIds()
            ok = self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(listing), ctypes.sizeof(listing), None)
            if ok and listing.count <= capacity and listing.assigned <= capacity:
                return [int(pid) for pid in listing.pids[:listing.count]]
            error = ctypes.get_last_error()
            if not ok and error != 234:  # ERROR_MORE_DATA
                raise ctypes.WinError(error)
            capacity *= 2
        raise WorkerError("worker_job_query_failed", "Worker job process list exceeded the safety bound")

    def refresh(self) -> list[int]:
        pids = self.active_pids()
        for pid in pids:
            try:
                process = psutil.Process(pid)
                identity = (pid, process.create_time())
                if identity not in self.members:
                    data = {"pid": pid, "create_time": identity[1], "ppid": process.ppid(),
                            "exe": process.exe(), "root": self.process is not None and pid == self.process.pid}
                    self.members[identity] = data
                    self.evidence.append("worker_job_member", call_id=self.call_id, **data)
            except psutil.NoSuchProcess:
                # It already exited; job accounting, not this observation,
                # determines whether all processes have terminated.
                pass
        return pids

    def terminate_and_confirm(self) -> None:
        if self.closed:
            return
        self.refresh()
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 5
        while True:
            remaining = self.active_pids()
            if not remaining:
                break
            if time.monotonic() >= deadline:
                raise WorkerError("worker_reap_failed", f"Worker job still has active PIDs: {remaining}")
            time.sleep(.02)
        # Job accounting can reach zero before a console helper's process
        # object becomes signaled. Confirm termination of observed identities
        # as well; never terminate a process selected only by a historical PID.
        for member in self.members.values():
            try:
                observed=psutil.Process(member['pid'])
                if observed.create_time()!=member['create_time']:
                    continue
                exit_code=observed.wait(timeout=max(0,deadline-time.monotonic()))
                self.evidence.append('worker_member_terminated',call_id=self.call_id,
                                     pid=member['pid'],create_time=member['create_time'],
                                     exit_code=exit_code)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired as exc:
                raise WorkerError('worker_reap_failed',f'Job member did not finish terminating: {member}') from exc
        if not self.kernel.CloseHandle(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = None
        self.closed = True
        self.evidence.append("worker_job_closed", call_id=self.call_id, active_processes_remaining=0,
                             observed_members=list(self.members.values()), application_terminated=False)


class WriterLease:
    """Nonblocking process-wide and cross-process lock for one R13 PID.

    Windows uses a kernel named mutex. POSIX uses flock only to support offline
    development; it does not make the Windows adapter portable. Acquire and
    release on the same thread, as required by Windows mutex ownership.
    """

    _registry: set[int] = set()
    _registry_lock = threading.Lock()

    def __init__(self, pid: int):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("R13 PID must be a positive integer")
        self.pid = pid
        self._handle = None
        self._stream = None
        self._owner_thread = None
        self.abandoned = False

    def acquire(self) -> "WriterLease":
        with self._registry_lock:
            if self._owner_thread is not None or self.pid in self._registry:
                raise WorkerError("writer_busy", f"Writer already holds R13 PID {self.pid}")
            self._registry.add(self.pid)
        try:
            if os.name == "nt":
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                kernel.CreateMutexW.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.WaitForSingleObject.restype = wintypes.DWORD
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.CloseHandle.restype = wintypes.BOOL
                handle = kernel.CreateMutexW(None, False, f"Local\\af_r13_bridge.writer.{self.pid}")
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                status = kernel.WaitForSingleObject(handle, 0)
                if status not in (0, 0x80):
                    kernel.CloseHandle(handle)
                    if status == 0x102:
                        raise WorkerError("writer_busy", f"Another process controls R13 PID {self.pid}")
                    raise WorkerError("writer_lock_failed", f"WaitForSingleObject returned {status}")
                self._handle = handle
                self.abandoned = status == 0x80
            else:
                import fcntl
                self._stream = (Path(tempfile.gettempdir()) / f"af_r13_bridge.writer.{self.pid}.lock").open("a+b")
                try:
                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise WorkerError("writer_busy", f"Another process controls R13 PID {self.pid}") from exc
            self._owner_thread = threading.get_ident()
            return self
        except BaseException:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            with self._registry_lock:
                self._registry.discard(self.pid)
            raise

    def release(self) -> None:
        if self._owner_thread is None:
            return
        if self._owner_thread != threading.get_ident():
            raise WorkerError("writer_thread_mismatch", "Release the writer lease on its owning thread")
        if os.name == "nt":
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel.ReleaseMutex.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            if not kernel.ReleaseMutex(self._handle):
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.CloseHandle(self._handle)
            self._handle = None
        else:
            import fcntl
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()
            self._stream = None
        self._owner_thread = None
        with self._registry_lock:
            self._registry.discard(self.pid)

    def __enter__(self) -> "WriterLease":
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


class Supervisor:
    def __init__(self, evidence_dir: str | Path, *,
                 command_factory: Callable[[Path, Path], list[str]] | None = None):
        """command_factory is an internal non-UI test seam, never a CLI option."""
        self.evidence = Evidence(evidence_dir)
        self._active: subprocess.Popen | None = None
        self._job: WindowsWorkerJob | None = None
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._reap_lock = threading.Lock()
        self._closed = False
        self._command_factory = command_factory

    @property
    def active_pid(self) -> int | None:
        with self._state_lock:
            return self._active.pid if self._active is not None else None

    def _reap(self, process: subprocess.Popen) -> None:
        """Terminate our private worker job and join its exact root Popen."""
        with self._reap_lock:
            try:
                job = self._job if self._active is process else None
                if job is not None:
                    job.terminate_and_confirm()
                # Also covers failure before job assignment. Such a Windows
                # root is still suspended and has executed no child-spawning code.
                if process.poll() is None:
                    try:
                        process.kill()
                    except OSError:
                        if process.poll() is None:
                            raise
                process.wait(timeout=5)
                if job is not None:
                    self._job = None
            except (OSError, psutil.Error, subprocess.TimeoutExpired, WorkerError) as exc:
                with self._state_lock:
                    self._closed = True
                self.evidence.append("worker_reap_failed", worker_pid=process.pid,
                                     error=str(exc), future_writes_blocked=True)
                raise WorkerError("worker_reap_failed", "Owned worker tree termination was not confirmed") from exc

    def _wait_for_worker(self, process: subprocess.Popen, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while True:
            job = self._job
            if job is not None:
                job.refresh()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            try:
                return process.wait(timeout=min(remaining, .05))
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise

    def run_worker(self, request: dict, timeout: float = 15) -> dict:
        if not isinstance(request, dict):
            raise TypeError("worker request must be a JSON object")
        if timeout <= 0:
            raise ValueError("worker timeout must be positive")
        if not self._run_lock.acquire(blocking=False):
            raise WorkerError("worker_busy", "A worker is already active in this supervisor")
        process = None
        job = None
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        paths = {kind: str(self.evidence.directory / f"worker-{call_id}.{kind}")
                 for kind in ("request.json", "response.json", "stdout.log", "stderr.log")}
        try:
            with self._state_lock:
                if self._closed:
                    raise WorkerError("supervisor_closed", "This supervisor has been shut down")
                if self._active is not None and self._active.poll() is None:
                    raise WorkerError("worker_busy", "An earlier owned worker is still alive")
            payload = dict(request)
            payload.setdefault("request_id", call_id)
            with Path(paths["request.json"]).open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            command = [sys.executable, "-I", "-m", "af_r13_bridge.adapters.uia_worker",
                       "--request", paths["request.json"], "--response", paths["response.json"]]
            if self._command_factory is not None:
                command = self._command_factory(Path(paths["request.json"]), Path(paths["response.json"]))
                if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
                    raise ValueError("test command_factory must return a nonempty list of strings")
            from .isolation import sanitized_environment
            self.evidence.append("worker_start", call_id=call_id,
                                 request_id=payload["request_id"], command=command,
                                 timeout_seconds=timeout, files=paths)
            if os.name == "nt":
                job = WindowsWorkerJob(self.evidence, call_id)
            with Path(paths["stdout.log"]).open("xb") as stdout, Path(paths["stderr.log"]).open("xb") as stderr:
                with self._state_lock:
                    if self._closed:
                        raise WorkerError("supervisor_closed", "Supervisor shut down before helper launch")
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                               stdout=stdout, stderr=stderr,
                                               cwd=self.evidence.directory,
                                               env=sanitized_environment(),
                                               creationflags=WindowsWorkerJob.creation_flags if os.name == "nt" else 0)
                    self._active = process
                    self._job = job
                if job is not None:
                    job.assign_and_resume(process)
                try:
                    returncode = self._wait_for_worker(process, timeout)
                except subprocess.TimeoutExpired as exc:
                    raise WorkerError("worker_timeout", f"UI probe exceeded {timeout:g} seconds", evidence=paths) from exc
                # A successful root exit cannot leave a redirected interpreter
                # or descendant running. Job completion is a separate gate.
                self._reap(process)
                if returncode != 0:
                    raise WorkerError("worker_exit", f"UI worker exited with code {returncode}", evidence=paths)
            try:
                response = json.loads(Path(paths["response.json"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise WorkerError("worker_response_invalid", "UI worker did not produce valid response JSON", evidence=paths) from exc
            if not isinstance(response, dict):
                raise WorkerError("worker_response_invalid", "UI worker response is not an object", evidence=paths)
            self.evidence.append("worker_end", call_id=call_id, returncode=returncode,
                                 elapsed_ms=round((time.monotonic() - started) * 1000, 3),
                                 response_file=paths["response.json"])
            return response
        except BaseException as exc:
            if process is not None:
                self._reap(process)
            elif job is not None:
                job.terminate_and_confirm()
            self.evidence.append("worker_failed", call_id=call_id,
                                 error_type=type(exc).__name__, error=str(exc),
                                 worker_reaped=process is None or process.poll() is not None,
                                 elapsed_ms=round((time.monotonic() - started) * 1000, 3), files=paths)
            raise
        finally:
            with self._state_lock:
                if self._active is process and self._job is None and (process is None or process.poll() is not None):
                    self._active = None
            self._run_lock.release()

    def shutdown(self) -> None:
        """Prevent future writes and join any owned worker. Does not exit R13."""
        with self._state_lock:
            self._closed = True
            process = self._active
        if process is not None:
            self._reap(process)
            with self._state_lock:
                if self._active is process:
                    self._active = None
        self.evidence.append("supervisor_shutdown", worker_reaped=process is None or process.poll() is not None,
                             application_terminated=False)

    def __enter__(self) -> "Supervisor":
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown()
