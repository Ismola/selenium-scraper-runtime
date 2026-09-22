"""Small independent guard for browser processes owned by a scraper worker.

It survives a worker crash and removes the local browser tree. The guard also
caps the session lifetime so a caller that forgets to close a driver cannot
leave it running indefinitely.
"""

import os
import sys
import time

import psutil


def _same_process(pid, created):
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) < 0.01 and process.status() != psutil.STATUS_ZOMBIE:
            return process
    except psutil.Error:
        pass
    return None


def _stop_processes(processes):
    alive = []
    for process in processes.values():
        try:
            if process.is_running():
                alive.append(process)
        except psutil.Error:
            pass
    for process in alive:
        try:
            process.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(alive, timeout=2)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=2)


def supervise(owner_pid, owner_created, service_pid, service_created, max_lifetime,
              initial_processes=()):
    known = {}
    for pid, created in initial_processes:
        process = _same_process(pid, created)
        if process is not None:
            known[(pid, created)] = process
    deadline = time.monotonic() + max_lifetime
    while True:
        service = _same_process(service_pid, service_created)
        if service is not None:
            try:
                for process in service.children(recursive=True) + [service]:
                    known[(process.pid, process.create_time())] = process
            except psutil.Error:
                pass
        if os.name == "posix":
            for process in psutil.process_iter(["pid"]):
                try:
                    if (os.getpgid(process.pid) == service_pid
                            and process.create_time() >= service_created - 1):
                        known[(process.pid, process.create_time())] = process
                except (OSError, psutil.Error):
                    pass
        owner_alive = _same_process(owner_pid, owner_created) is not None
        if not owner_alive or time.monotonic() >= deadline or service is None:
            _stop_processes(known)
            return
        time.sleep(0.5)


if __name__ == "__main__":
    initial = [tuple(map(float, item.split(":"))) for item in sys.argv[6:]]
    supervise(int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]),
              float(sys.argv[4]), int(sys.argv[5]),
              [(int(pid), created) for pid, created in initial])
