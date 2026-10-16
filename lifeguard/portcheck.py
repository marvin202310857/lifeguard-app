"""Is the LifeGuard port free?   python -m lifeguard.portcheck 5000 [--kill]

Windows lets two programs listen on the same port without any warning, so an old copy of the
system that starts at boot can quietly answer the browser instead of the new one. This tool
shows which program holds the port and what started it, and can stop it.

Exit code: 0 = port is free, 1 = something is listening on it.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time

IS_WINDOWS = sys.platform == "win32"

# Programs of the previous LifeGuard versions: safe to stop automatically. Anything else is left alone.
OLD_VERSION = re.compile(r"pool_safety|vision_service|LifeGuard\.js|index\.js", re.I)
THIS_VERSION = re.compile(r"LifeGuard\.pyw|lifeguard\.serve|lifeguard\.app|run\.py", re.I)


def is_listening(port, host="127.0.0.1"):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) == 0


def listening_pids(port, netstat_text=None):
    """PIDs listening on `port`, parsed from `netstat -ano -p tcp` output (Windows)."""
    if netstat_text is None:
        netstat_text = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                      capture_output=True, text=True, timeout=20).stdout
    pids = set()
    for line in netstat_text.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING"
                and parts[1].rsplit(":", 1)[-1] == str(port) and parts[4].isdigit()):
            pids.add(int(parts[4]))
    return sorted(pids)


def process_info(pid):
    """{'ProcessId', 'ParentProcessId', 'Name', 'CommandLine'} or None (Windows)."""
    query = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | "
             "Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return json.loads(out) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def started_by(parent):
    """Plain-English guess of what launched a process, from its parent."""
    if not parent:
        return "unknown (its parent has already exited, which is typical for a scheduled task or startup script)"
    name, cmd = (parent.get("Name") or "").lower(), (parent.get("CommandLine") or "").lower()
    if name == "svchost.exe" and "schedule" in cmd:
        return "Task Scheduler - open Task Scheduler (taskschd.msc) and disable the task"
    if name == "explorer.exe":
        return "a Startup shortcut, or someone double-clicked it - check shell:startup (Win+R)"
    if name in ("cmd.exe", "powershell.exe", "pwsh.exe", "wscript.exe"):
        return f"a script ({parent.get('CommandLine') or name}) - find and delete that .bat/.ps1/.vbs file"
    if name in ("code.exe", "node.exe") or "code" in name:
        return "VS Code or a Node tool - check .vscode/tasks.json and your terminal tabs"
    return f"{parent.get('Name')} ({parent.get('CommandLine') or 'no command line'})"


def describe_holders(port):
    for pid in listening_pids(port):
        info = process_info(pid)
        print(f"  PID {pid}: {(info or {}).get('Name', '?')}")
        print(f"    command : {(info or {}).get('CommandLine') or '(not available)'}")
        parent = process_info(info["ParentProcessId"]) if info and info.get("ParentProcessId") else None
        print(f"    started by: {started_by(parent)}")


def _kill(pid):
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


def kill_holders(port):
    for pid in listening_pids(port):
        if pid != os.getpid():
            _kill(pid)


def stop_old_versions(port, log=print, wait_sec=6):
    """Free the port for this version. -> "free" | "ours" (already running) | "busy" (other program)"""
    if not is_listening(port):
        return "free"
    if not IS_WINDOWS:
        return "busy"
    for pid in listening_pids(port):
        if pid == os.getpid():
            continue
        command = (process_info(pid) or {}).get("CommandLine") or ""
        if THIS_VERSION.search(command):
            return "ours"
        if OLD_VERSION.search(command):
            log(f"Stopping the old LifeGuard on port {port}: PID {pid} ({command})")
            _kill(pid)
        else:
            log(f"Port {port} is used by PID {pid}: {command or 'unknown program'}")
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if not is_listening(port):
            return "free"
        time.sleep(0.3)
    return "free" if not is_listening(port) else "busy"


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    port = int(args[0]) if args else 5000
    if not is_listening(port):
        print(f"Port {port} is free.")
        return 0

    print(f"Port {port} is already in use by another program:")
    if IS_WINDOWS:
        describe_holders(port)
    else:
        print(f"  find it with:  lsof -i :{port}")
    if "--kill" in argv:
        if IS_WINDOWS:
            kill_holders(port)
        return 1 if is_listening(port) else 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
