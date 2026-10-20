"""Linux process ownership: exact birth identities, descendants and inherited tokens."""
import os
from pathlib import Path
import signal
import time

OWNER_ENV = 'WORKLANE_PROCESS_OWNER'


def snapshot():
    processes = {}
    for path in Path('/proc').glob('[0-9]*/stat'):
        try:
            fields = path.read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                processes[int(path.parent.name)] = (fields[19], int(fields[1]), int(fields[2]))
        except (OSError, ValueError, IndexError):
            continue
    return processes


def identity(pid):
    return snapshot().get(pid, (None,))[0] if pid else None


def owned(roots=(), tokens=(), known=None):
    processes = snapshot()
    result = {pid: birth for pid, birth in (known or {}).items()
              if birth and processes.get(pid, (None,))[0] == birth}
    for pid, birth in roots:
        if birth and processes.get(pid, (None,))[0] == birth:
            result[pid] = birth
    tokens = {f'{OWNER_ENV}={token}'.encode() for token in tokens if token}
    if tokens:
        for pid, (birth, _, _) in processes.items():
            try:
                if tokens.intersection(Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')):
                    result[pid] = birth
            except OSError:
                continue
    # Include same-group children after their leader exits, and detached descendants
    # before reparenting. Never infer ownership from command names or checkout paths.
    groups = {pid for pid in result if processes.get(pid, (None, None, None))[2] == pid}
    changed = True
    while changed:
        changed = False
        for pid, (birth, parent, group) in processes.items():
            if pid not in result and (parent in result or group in groups):
                result[pid] = birth
                changed = True
    return {pid: birth for pid, birth in result.items()
            if pid != os.getpid() and processes.get(pid, (None,))[0] == birth}


def signal_owned(processes, sig):
    current = snapshot()
    for pid, birth in processes.items():
        if birth and current.get(pid, (None,))[0] == birth:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


def terminate_owned(roots=(), tokens=(), grace=2, known=None):
    known = owned(roots, tokens, known)
    deadline = time.monotonic() + grace
    while known:
        signal_owned(known, signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL)
        time.sleep(.05)
        known = owned(roots, tokens, known)
        if time.monotonic() > deadline + 2:
            break
    return sorted(known)
