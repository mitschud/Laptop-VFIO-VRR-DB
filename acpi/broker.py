#!/usr/bin/env python3
"""Serve bounded NVPCF snapshots over a dedicated QEMU serial channel."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import grp
import os
from pathlib import Path
import re
import socket
import struct
from threading import Event, Thread
import time


REQUEST = b"\xa6"
HEADER = 0xA5
VALID_POWER = 1
VALID_TEMPERATURES = 2
AC_ONLINE = 4
FIELDS = ("ACBT", "ATPP", "AMAT", "AMIT", "DBAC", "DBDC")
CPU_TEMP = r"\_SB.PC00.LPCB.EC0.CPUT"
GPU_TEMP = r"\_SB.PC00.LPCB.EC0.GPDT"


@dataclass(frozen=True)
class Snapshot:
    online: bool
    power: tuple[int, int, int, int, int, int] | None
    temperatures: tuple[int, int] | None

    def encode(self, sequence):
        flags = (AC_ONLINE if self.online else 0)
        if self.power is not None:
            flags |= VALID_POWER
        if self.temperatures is not None:
            flags |= VALID_TEMPERATURES
        body = struct.pack("<BBB4H4B", HEADER, sequence, flags, *(self.power or (0,) * 6),
                           *(self.temperatures or (0, 0)))
        return body + bytes(((-sum(body)) & 0xFF,))


class Firmware:
    """A fixed allowlist of integer reads, with no guest arguments."""

    def __init__(self, control):
        self.control = control

    def read(self, name):
        if name not in (CPU_TEMP, GPU_TEMP) and name not in tuple(
            rf"\_SB.NPCF.{field}" for field in FIELDS
        ):
            raise ValueError("Unexpected ACPI path")
        self.control.write_text(name + "\n")
        result = self.control.read_text().strip().rstrip("\x00")
        if re.fullmatch(r"0x[0-9a-fA-F]+", result) is None:
            raise ValueError(f"{name} did not return an integer")
        return int(result, 16)


def online_state(path):
    state = path.read_text().strip()
    if state not in ("0", "1"):
        raise ValueError("Unrecognized AC status")
    return state == "1"


def collect(firmware, ac_path):
    online = online_state(ac_path)
    power = None
    temperatures = None
    try:
        values = tuple(firmware.read(rf"\_SB.NPCF.{name}") for name in FIELDS)
        if all(0 <= value <= 0xFFFF for value in values[:4]) and all(
            flag in (0, 1) for flag in values[4:]
        ):
            power = values
    except (OSError, ValueError):
        pass
    try:
        values = (firmware.read(CPU_TEMP), firmware.read(GPU_TEMP))
        if all(0 < value <= 125 for value in values):
            temperatures = values
    except (OSError, ValueError):
        pass
    if online_state(ac_path) != online:
        return Snapshot(False, None, None)
    return Snapshot(online, power, temperatures)


def serve(connection, current_snapshot):
    awaiting_sequence = False
    while chunk := connection.recv(256):
        for byte in chunk:
            if awaiting_sequence:
                connection.sendall(current_snapshot().encode(byte))
                awaiting_sequence = False
            elif byte == REQUEST[0]:
                awaiting_sequence = True


@contextmanager
def open_channel(path, listen, fixture):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        if not listen:
            sock.connect(str(path))
            yield sock
            return
        sock.bind(str(path))
        try:
            if not fixture:
                os.chown(path, 0, grp.getgrnam("libvirt-qemu").gr_gid)
            path.chmod(0o660)
            sock.listen(1)
            path.with_name('ready').touch(mode=0o600)
            connection, _ = sock.accept()
            with connection:
                print("QEMU NVPCF UART connected", flush=True)
                yield connection
        finally:
            path.with_name('ready').unlink(missing_ok=True)
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("socket", type=Path, help="Socket exported by the QEMU chardev")
    parser.add_argument("--fixture", action="store_true", help="Serve synthetic test data")
    parser.add_argument("--listen", action="store_true", help="Listen on a root-managed socket")
    args = parser.parse_args()
    firmware = Firmware(Path("/proc/acpi/call"))
    ac_path = Path("/sys/class/power_supply/ADP0/online")
    if not args.fixture:
        dmi = Path("/sys/class/dmi/id")
        if not (dmi / "product_name").read_text().strip().startswith("82TD"):
            parser.error("The live firmware backend is specific to Legion 82TD")
        if (dmi / "bios_version").read_text().strip() != "K1CN40WW":
            parser.error("The live firmware backend requires the inspected K1CN40WW BIOS")
    cached = float("-inf"), Snapshot(False, None, None)
    last_reported = None
    stop = Event()

    def sample_once():
        nonlocal cached, last_reported
        try:
            state = collect(firmware, ac_path)
        except (OSError, ValueError):
            state = Snapshot(False, None, None)
        cached = time.monotonic(), state
        available = state.online, state.power is not None, state.temperatures is not None
        if available != last_reported:
            print(f"NVPCF AC={available[0]} budgets={available[1]} temperatures={available[2]}", flush=True)
            last_reported = available

    def sample():
        while not stop.wait(1):
            sample_once()

    def snapshot():
        if args.fixture:
            return Snapshot(True, (400, 440, 200, 0, 1, 1), (52, 61))
        sampled_at, state = cached
        if time.monotonic() - sampled_at > 2:
            return Snapshot(False, None, None)
        try:
            if online_state(ac_path) == state.online:
                return state
        except (OSError, ValueError):
            pass
        return Snapshot(False, None, None)

    if not args.fixture:
        sample_once()
        if not (cached[1].online and cached[1].power is not None):
            raise SystemExit('Initial NVPCF AC power data is unavailable')
    with open_channel(args.socket, args.listen, args.fixture) as connection:
        if not args.fixture:
            Thread(target=sample, name="nvpcf-firmware-sampler", daemon=True).start()
        try:
            serve(connection, snapshot)
        finally:
            stop.set()


if __name__ == "__main__":
    main()
