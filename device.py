"""Clickr device: a fake 2-button USB mouse, served over USB/IP.

usbip-win2 plugs it in, Windows treats it as a real HID mouse.
Ports (localhost only):
    3240  USB/IP (usbip.exe attaches bus id 1-1)
    3241  control: L1/L2 = left down/up, R1/R2 = right down/up, release, status
"""
import argparse
import asyncio
import logging
import struct
from collections import deque
from pathlib import Path

HOST = "127.0.0.1"
USBIP_PORT = 3240  # clickr.ps1 has its own copy of both ports. Change one, change both.
CONTROL_PORT = 3241

USBIP_VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
EPIPE, ECONNRESET = -32, -104

BUSNUM, DEVNUM, BUSID = 1, 1, b"1-1"
SPEED_FULL = 2
VID, PID, BCD_DEVICE = 0x1209, 0x0001, 0x0100  # pid.codes test IDs. Fine for private use only.

# 2 buttons, 6 bits padding, X, Y. Windows ignores a "mouse" without X/Y, so they exist but stay 0.
REPORT_DESC = bytes([
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01,  # Generic Desktop / Mouse / Application
    0x09, 0x01, 0xA1, 0x00,              # Pointer / Physical
    0x05, 0x09, 0x19, 0x01, 0x29, 0x02,  # Buttons 1-2
    0x15, 0x00, 0x25, 0x01, 0x95, 0x02, 0x75, 0x01, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x06, 0x81, 0x03,  # padding
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31,  # X, Y
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x02, 0x81, 0x06,
    0xC0, 0xC0,
])

DEVICE_DESC = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0, 0, 0, 64, VID, PID, BCD_DEVICE, 1, 2, 3, 1)
INTERFACE_DESC = bytes([9, 4, 0, 0, 1, 0x03, 0x01, 0x02, 0])  # HID boot mouse
HID_DESC = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, len(REPORT_DESC))
CONFIG_DESC = b""  # set in main(): depends on --poll-ms
STRINGS = {1: "Clickr", 2: "Clickr Virtual Mouse", 3: "CLICKR01"}

BUTTONS = {"left": 0x01, "right": 0x02}
ACTIONS = {"l1": ("left", True), "l2": ("left", False), "r1": ("right", True), "r2": ("right", False)}

log = logging.getLogger("clickr")


def build_config_desc(poll_ms):
    endpoint = struct.pack("<BBBBHB", 7, 5, 0x81, 0x03, 4, poll_ms)
    body = INTERFACE_DESC + HID_DESC + endpoint
    return struct.pack("<BBHBBBBB", 9, 2, 9 + len(body), 1, 1, 0, 0x80, 50) + body


def string_desc(index):
    if index == 0:
        return bytes([4, 3, 0x09, 0x04])
    text = STRINGS.get(index)
    if text is None:
        return None
    raw = text.encode("utf-16-le")
    return bytes([2 + len(raw), 3]) + raw


def device_info():
    return (b"/sys/devices/clickr/1-1".ljust(256, b"\0") + BUSID.ljust(32, b"\0")
            + struct.pack(">IIIHHHBBBBBB", BUSNUM, DEVNUM, SPEED_FULL, VID, PID, BCD_DEVICE, 0, 0, 0, 1, 1, 1))


class Mouse:
    def __init__(self):
        self.buttons = 0
        self.writer = None
        self.reports = deque()  # button states Windows hasn't asked for yet
        self.pending = deque()  # Windows' open read requests, waiting for a state change
        self.idle = 0
        self.protocol = 1

    @property
    def attached(self):
        return self.writer is not None

    def report(self):
        return bytes([self.buttons, 0, 0])

    def attach(self, writer):
        self.writer = writer
        self.reports.clear()
        self.pending.clear()

    def detach(self):
        self.writer = None
        self.buttons = 0
        self.reports.clear()
        self.pending.clear()

    def set_button(self, name, pressed):
        bit = BUTTONS[name]
        new = self.buttons | bit if pressed else self.buttons & ~bit
        if new != self.buttons:
            self.buttons = new
            self.reports.append(self.report())
            self.flush()

    def release_all(self):
        if self.buttons:
            self.buttons = 0
            self.reports.append(self.report())
            self.flush()

    def flush(self):
        while self.writer and self.reports and self.pending:
            seqnum, packets = self.pending.popleft()
            self.ret_submit(seqnum, 0, self.reports.popleft(), packets)

    def ret_submit(self, seqnum, status, data=b"", packets=0, actual=None):
        actual = len(data) if actual is None else actual
        self.writer.write(struct.pack(">IIIIIiiiii8x", RET_SUBMIT, seqnum, 0, 0, 0, status, actual, 0, packets, 0) + data)

    def ret_unlink(self, seqnum, status):
        self.writer.write(struct.pack(">IIIIIi24x", RET_UNLINK, seqnum, 0, 0, 0, status))

    def submit(self, seqnum, ep, direction, length, packets, setup, data):
        if ep == 0:
            status, reply = self.control(setup, data)
            if direction == 1:
                self.ret_submit(seqnum, status, reply[:length], packets)
            else:
                self.ret_submit(seqnum, status, b"", packets, actual=len(data) if status == 0 else 0)
        elif ep == 1 and direction == 1:
            # Don't answer until a button changes. Answering early = Windows polls in a hot loop.
            self.pending.append((seqnum, packets))
            self.flush()
        else:
            self.ret_submit(seqnum, EPIPE, b"", packets)

    def unlink(self, seqnum, target):
        for item in self.pending:
            if item[0] == target:
                self.pending.remove(item)
                self.ret_unlink(seqnum, ECONNRESET)
                return
        self.ret_unlink(seqnum, 0)

    def control(self, setup, data):
        rtype, req, value, index, length = struct.unpack("<BBHHH", setup)
        dtype, didx = value >> 8, value & 0xFF
        log.debug("control type=%02x req=%02x value=%04x index=%04x len=%d", rtype, req, value, index, length)

        if req == 0x06 and rtype in (0x80, 0x81):  # GET_DESCRIPTOR
            desc = string_desc(didx) if dtype == 3 else {1: DEVICE_DESC, 2: CONFIG_DESC, 0x21: HID_DESC, 0x22: REPORT_DESC}.get(dtype)
            return (0, desc) if desc is not None else (EPIPE, b"")
        if req == 0x00 and rtype in (0x80, 0x81, 0x82):  # GET_STATUS
            return 0, b"\x00\x00"
        if rtype == 0x80 and req == 0x08:  # GET_CONFIGURATION
            return 0, b"\x01"
        if rtype == 0x81 and req == 0x0A:  # GET_INTERFACE
            return 0, b"\x00"
        if rtype == 0xA1 and req == 0x01:  # HID GET_REPORT
            return 0, self.report()
        if rtype == 0xA1 and req == 0x02:  # HID GET_IDLE
            return 0, bytes([self.idle])
        if rtype == 0xA1 and req == 0x03:  # HID GET_PROTOCOL
            return 0, bytes([self.protocol])
        if rtype == 0x21 and req == 0x0A:  # HID SET_IDLE
            self.idle = value >> 8
            return 0, b""
        if rtype == 0x21 and req == 0x0B:  # HID SET_PROTOCOL
            self.protocol = value & 0xFF
            return 0, b""
        if rtype & 0x80 == 0:  # any other OUT request: just say yes
            return 0, b""
        return EPIPE, b""


MOUSE = Mouse()


async def handle_usbip(reader, writer):
    peer = writer.get_extra_info("peername")
    attached_here = False
    try:
        _version, code, _status = struct.unpack(">HHI", await reader.readexactly(8))
        if code == OP_REQ_DEVLIST:
            writer.write(struct.pack(">HHII", USBIP_VERSION, OP_REP_DEVLIST, 0, 1)
                         + device_info() + bytes([0x03, 0x01, 0x02, 0x00]))
            await writer.drain()
            return
        if code != OP_REQ_IMPORT:
            log.warning("unknown op %#06x from %s", code, peer)
            return
        busid = (await reader.readexactly(32)).rstrip(b"\0")
        if busid != BUSID or MOUSE.attached:
            log.warning("import of %r refused (attached=%s)", busid, MOUSE.attached)
            writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 1))
            await writer.drain()
            return
        writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 0) + device_info())
        await writer.drain()
        MOUSE.attach(writer)
        attached_here = True
        log.info("attached by %s", peer)

        while True:
            header = await reader.readexactly(48)
            command, seqnum, _devid, direction, ep = struct.unpack(">IIIII", header[:20])
            if command == CMD_SUBMIT:
                _flags, length, _start, packets, _interval = struct.unpack(">Iiiii", header[20:40])
                data = await reader.readexactly(length) if direction == 0 and length > 0 else b""
                if packets > 0:  # isochronous junk; a mouse never gets any
                    await reader.readexactly(16 * packets)
                MOUSE.submit(seqnum, ep, direction, length, packets, header[40:48], data)
            elif command == CMD_UNLINK:
                (target,) = struct.unpack(">I", header[20:24])
                MOUSE.unlink(seqnum, target)
            else:
                log.warning("unknown command %d, dropping connection", command)
                break
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        if attached_here:
            MOUSE.detach()
            log.info("detached")
        writer.close()


async def handle_control(reader, writer):
    async for raw in reader:
        words = raw.decode(errors="replace").split()
        writer.write((run_command(words[0].lower()) if words else "err empty command").encode() + b"\n")
        await writer.drain()
    writer.close()


def run_command(cmd):
    if cmd == "status":
        state = " ".join(f"{name}={int(bool(MOUSE.buttons & bit))}" for name, bit in BUTTONS.items())
        return f"ok {'attached' if MOUSE.attached else 'detached'} {state}"
    if cmd == "release":
        MOUSE.release_all()
        return "ok"
    if cmd not in ACTIONS:
        return f"err unknown command {cmd!r} (use L1, L2, R1, R2, release, status)"
    if not MOUSE.attached:
        return "err device not attached (run: clickr.ps1 start)"
    MOUSE.set_button(*ACTIONS[cmd])
    return "ok"


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true", help="log every control request")
    parser.add_argument("--poll-ms", type=int, default=1, choices=range(1, 256), metavar="1-255",
                        help="how often Windows polls the mouse, in ms (default 1)")
    args = parser.parse_args()
    global CONFIG_DESC
    CONFIG_DESC = build_config_desc(args.poll_ms)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(Path(__file__).with_name("device.log")), logging.StreamHandler()],
    )
    # usbip.exe resets the socket on detach. That's normal; don't dump a traceback into the log for it.
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda lp, ctx: None if isinstance(ctx.get("exception"), ConnectionResetError)
                               else lp.default_exception_handler(ctx))
    usbip = await asyncio.start_server(handle_usbip, HOST, USBIP_PORT)
    control = await asyncio.start_server(handle_control, HOST, CONTROL_PORT)
    log.info("listening: usbip %s:%d, control %s:%d, polling %d ms", HOST, USBIP_PORT, HOST, CONTROL_PORT, args.poll_ms)
    async with usbip, control:
        await asyncio.gather(usbip.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
