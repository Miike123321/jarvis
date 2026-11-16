"""Installed only in the temporary test copy, before unittest imports anything."""
import sys


def _guard(event, args):
    del args
    if event in {
        "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
        "socket.getaddrinfo", "socket.gethostbyname",
        "socket.gethostbyaddr", "subprocess.Popen", "os.system", "os.exec",
        "os.posix_spawn", "os.spawn", "os.fork", "os.forkpty",
    }:
        raise RuntimeError("External networking and process creation are disabled by test isolation")


sys.addaudithook(_guard)
