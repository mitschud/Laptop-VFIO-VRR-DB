#!/usr/bin/env python3
"""Switch the patched Q35 bridge IDs during a running Windows boot."""
import argparse
import json
import os
import re
import struct
import subprocess
import time

VM = os.environ.get("LEGION_VFIO_VM", "windows-iot-ltsc")
MONITOR = ["virsh", "-c", "qemu:///system", "qemu-monitor-command", VM]
KERNEL_IMAGE = (0xFDA9ED74, 0x1450000)
# Microsoft ntkrnlmp.pdb C29EBFB06B78B3C020DCA66D99713F9E1.
IDT_HANDLERS = {
    0: (0x6B9800, 0xBC9100),
    13: (0x6BD1C0, 0xBC97C0),
    14: (0x6BD540, 0xBC9840),
}
CAPTURE_SECONDS = 45


def monitor(command):
    return subprocess.check_output(MONITOR + ["--hmp", command], text=True, timeout=4).strip()


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def kernel_address(address):
    return 0xFFFF800000000000 <= address < 0xFFFFFFFFFFFFFFFF


def image_matches(read_memory, addr, expected):
    try:
        dos = read_memory(addr, 64)
        pe_offset = struct.unpack_from("<I", dos, 60)[0]
        if dos[:2] != b"MZ" or not 64 <= pe_offset < 4096:
            return False
        pe = read_memory(addr + pe_offset, 84)
    except ValueError:
        return False
    return (pe[:6] == b"PE\0\0\x64\x86"
            and (struct.unpack_from("<I", pe, 8)[0], struct.unpack_from("<I", pe, 80)[0]) == expected)


def find_kernel(read_memory):
    registers = monitor("info registers")
    match = re.search(r"(?m)^IDT=\s*([0-9a-fA-F]+)\s+([0-9a-fA-F]+)", registers)
    if not match:
        return None
    idt, limit = (int(value, 16) for value in match.groups())
    if not kernel_address(idt) or limit < (max(IDT_HANDLERS) + 1) * 16 - 1:
        return None
    for vector, rvas in IDT_HANDLERS.items():
        try:
            lo, _, _, mid, hi, _ = struct.unpack("<HHHHII", read_memory(idt + vector * 16, 16))
        except ValueError:
            continue
        handler = lo | (mid << 16) | (hi << 32)
        for rva in rvas:
            base = handler - rva
            if kernel_address(base) and base % 4096 == 0 and image_matches(read_memory, base, KERNEL_IMAGE):
                return base
    return None


def switch_bridge_ids():
    request = {
        "execute": "qom-set",
        "arguments": {
            "path": "mch",
            "property": "x-legion-host-bridge-ids",
            "value": True,
        },
    }
    reply = json.loads(subprocess.check_output(
        MONITOR + [json.dumps(request)], text=True, timeout=4))
    if "return" not in reply:
        raise RuntimeError(f"Could not switch the Q35 host bridge IDs: {reply}")
    log("legion_bridge_ids_enabled")


def read_live_memory(addr, size):
    result = monitor(f"x /{size}bx 0x{addr:x}")
    data = bytearray()
    for line in result.splitlines():
        _, separator, values = line.partition(":")
        if separator:
            data.extend(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})(?![0-9a-fA-F])", values))
    if len(data) != size:
        raise ValueError(f"Cannot read {size} bytes from live vCPU at {addr:#x}")
    return bytes(data)


def run_live(probe_only):
    deadline = time.monotonic() + CAPTURE_SECONDS
    while time.monotonic() < deadline:
        kernel = find_kernel(read_live_memory)
        if kernel is not None:
            log("windows_kernel_matched_live", base=hex(kernel))
            if not probe_only:
                switch_bridge_ids()
            return
        time.sleep(0.08)
    raise RuntimeError("The running guest did not expose the verified Windows kernel in time")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-only", action="store_true", help="Find the kernel without switching the bridge IDs")
    arguments = parser.parse_args()
    try:
        run_live(arguments.probe_only)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error
