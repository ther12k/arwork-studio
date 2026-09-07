#!/usr/bin/env python3
"""Double-fork daemon launcher for sandbox services.

Tool-command spawns are reaped when the command's process tree is cleaned up.
A classic double-fork + setsid detaches the final child completely (reparented
to init) while the spawning command is still alive, so the service survives.

Usage:
  python3 daemon.py <workdir> <logfile> -- <command...>

Example (Next.js dev server):
  python3 daemon.py /home/z/my-project /home/z/my-project/dev.log -- bun run dev

Prints the daemon PID, then exits. Output is appended to <logfile>.
"""

import os
import sys


def main() -> None:
    argv = sys.argv[1:]
    if "--" not in argv:
        print("usage: daemon.py <workdir> <logfile> -- <command...>", file=sys.stderr)
        sys.exit(2)

    head, cmd = argv[: argv.index("--")], argv[argv.index("--") + 1 :]
    if len(head) < 2 or not cmd:
        print("usage: daemon.py <workdir> <logfile> -- <command...>", file=sys.stderr)
        sys.exit(2)

    workdir, logfile = head[0], head[1]

    # ---- fork 1: parent exits immediately, child escapes the tool tree ----
    pid = os.fork()
    if pid > 0:
        print(pid)
        sys.exit(0)

    os.setsid()

    # ---- fork 2: guarantee the daemon can never reacquire a terminal ----
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # ---- detach stdio into the service logfile ----
    os.chdir(workdir)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    logfd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(logfd, 1)
    os.dup2(logfd, 2)
    os.close(logfd)

    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
