"""Talks to the Clickr device (127.0.0.1:3241).

    python mouse.py L1 | L2 | R1 | R2 | release | status

    from mouse import VirtualMouse
    with VirtualMouse() as m:
        m.l1(); m.l2()      # you decide how long to wait in between
"""
import socket
import sys

from device import CONTROL_PORT


class VirtualMouse:
    def __init__(self, host="127.0.0.1", port=CONTROL_PORT):
        self._sock = socket.create_connection((host, port), timeout=5)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._file = self._sock.makefile("rw", encoding="ascii", newline="\n")

    def send(self, command):
        self._file.write(command + "\n")
        self._file.flush()
        reply = self._file.readline().strip()
        if not reply.startswith("ok"):
            raise RuntimeError(reply or "no reply from device")
        return reply

    def l1(self):
        self.send("L1")

    def l2(self):
        self.send("L2")

    def r1(self):
        self.send("R1")

    def r2(self):
        self.send("R2")

    def release(self):
        self.send("release")

    def status(self):
        return self.send("status")

    def close(self):
        self._file.close()
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    try:
        with VirtualMouse() as mouse:
            print(mouse.send(" ".join(sys.argv[1:])))
    except ConnectionRefusedError:
        sys.exit("Clickr device isn't running. Run: clickr.ps1 start")
    except RuntimeError as e:
        sys.exit(str(e))
