"""Process cleanup helpers that do not depend on Linux pidfd support."""

import time

import psutil


def alive_processes(processes):
    """Return live, non-zombie processes without letting inspection errors escape."""
    alive = []
    for process in processes:
        try:
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                alive.append(process)
        except (OSError, psutil.Error):
            pass
    return alive


def wait_for_processes(processes, timeout):
    """Poll until processes stop, avoiding psutil's pidfd based wait path."""
    deadline = time.monotonic() + timeout
    alive = alive_processes(processes)
    while alive and time.monotonic() < deadline:
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        alive = alive_processes(alive)
    return alive


def stop_processes(processes, terminate_timeout=2, kill_timeout=2):
    """Terminate, then kill, a known process set. Cleanup errors never escape."""
    alive = alive_processes(processes)
    for process in alive:
        try:
            process.terminate()
        except (OSError, psutil.Error):
            pass
    alive = wait_for_processes(alive, terminate_timeout)
    for process in alive:
        try:
            process.kill()
        except (OSError, psutil.Error):
            pass
    return wait_for_processes(alive, kill_timeout)
