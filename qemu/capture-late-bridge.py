#!/usr/bin/python3
"""Capture the Windows 26100/NVIDIA 621.01 firmware-object lookup from QEMU."""
from collections import namedtuple
import grp
import hashlib
import json
import os
from pathlib import Path
import re
import select
import socket
import struct
import subprocess
import sys
import tempfile
import time

VM = os.environ.get("LEGION_VFIO_VM", "windows-iot-ltsc")
MONITOR = ["virsh", "-c", "qemu:///system", "qemu-monitor-command", VM]
KERNEL_IMAGE = (0xFDA9ED74, 0x1450000)
DRIVER_IMAGE = (0x6AD5EBDE, 0x6211000)
# Microsoft ntkrnlmp.pdb C29EBFB06B78B3C020DCA66D99713F9E1.
IDT_HANDLERS = {
    0: (0x6B9800, 0xBC9100),
    13: (0x6BD1C0, 0xBC97C0),
    14: (0x6BD540, 0xBC9840),
}
MODULE_LIST = 0xEF5410
LOOKUP_RETURN = 0x16FB6B
LOOKUP_BYTES = bytes.fromhex("85c075278b4c2468b8ffff000085")
# Windows 621.01 caller stack and instructions after fetching the VK payload.
VK_FULL_READ_CALLER = 0x31E65B
VK_PARSER_RETURN = 0x702B64
VK_COMPARISON_RETURN = 0x702428
VK_COMPARISON_WORDS = 0x701091
# Stack distance back to the original VK caller, including return addresses.
VK_FOLLOWUP = {
    VK_COMPARISON_RETURN: ("hardware_comparison_return", 0x4A0, bytes.fromhex("85c00f8431ffffff")),
    VK_COMPARISON_WORDS: ("hardware_comparison_words", 0x510, bytes.fromhex("66413b6d1c0f94c133c0")),
    VK_PARSER_RETURN: ("key_parser_return", 0xE0, bytes.fromhex("488b4b0841b80100000084c0")),
}
MAX_DETAIL_HITS = 12
CAPTURE_SECONDS = 45
MAX_SAMPLES = 16


def monitor(command):
    return subprocess.check_output(MONITOR + ["--hmp", command], text=True, timeout=4).strip()


def monitor_status():
    data = subprocess.check_output(MONITOR + ['{"execute":"query-status"}'], text=True, timeout=4)
    return json.loads(data)["return"]


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def kernel_address(address):
    return 0xFFFF800000000000 <= address < 0xFFFFFFFFFFFFFFFF


class Remote:
    """The small GDB remote packet subset needed by the capture."""
    def __init__(self, conn):
        self.conn = conn
        self.pending = bytearray()
        self.breaks = set()
        self.running = False
        self.command("qSupported")
        self.command("?")

    def packet(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            start = self.pending.find(b"$")
            if start >= 0:
                end = self.pending.find(b"#", start + 1)
                if end >= 0 and len(self.pending) >= end + 3:
                    data = bytes(self.pending[start + 1:end])
                    checksum = int(self.pending[end + 1:end + 3], 16)
                    del self.pending[:end + 3]
                    if sum(data) % 256 != checksum:
                        self.conn.sendall(b"-")
                        continue
                    self.conn.sendall(b"+")
                    return data.decode("ascii")
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.conn], [], [], left)[0]:
                raise TimeoutError("No QEMU debugger response")
            chunk = self.conn.recv(65536)
            if not chunk:
                raise EOFError("QEMU closed the debugger socket")
            self.pending.extend(chunk)

    def send(self, command):
        data = command.encode("ascii")
        self.conn.sendall(b"$" + data + f"#{sum(data) % 256:02x}".encode("ascii"))

    def command(self, command):
        self.send(command)
        return self.packet(3)

    def memory(self, addr, size):
        result = self.command(f"m{addr:x},{size:x}")
        if len(result) != size * 2:
            raise ValueError(f"Cannot read {size} bytes from {addr:#x}")
        return bytes.fromhex(result)

    def breakpoint(self, kind, addr, size, enable=True):
        key = (kind, addr, size)
        result = self.command(f"{'Z' if enable else 'z'}{kind},{addr:x},{size:x}")
        if result != "OK":
            raise RuntimeError(f"QEMU rejected hardware breakpoint {key}: {result}")
        if enable:
            self.breaks.add(key)
        else:
            self.breaks.discard(key)

    def step_guest(self, seconds, command="c"):
        self.send(command)
        self.running = True
        try:
            result = self.packet(seconds)
        except TimeoutError:
            self.conn.sendall(b"\x03")
            result = self.packet(3)
        self.running = False
        if not result.startswith(("S", "T")):
            raise RuntimeError(f"Unexpected debugger stop packet: {result}")
        return result

    def detach(self):
        if self.running:
            self.conn.sendall(b"\x03")
            self.packet(3)
            self.running = False
        for kind, addr, size in tuple(self.breaks):
            self.breakpoint(kind, addr, size, enable=False)
        self.command("D")


def image_matches(remote, addr, expected):
    try:
        dos = remote.memory(addr, 64)
        pe_offset = struct.unpack_from("<I", dos, 60)[0]
        if dos[:2] != b"MZ" or not 64 <= pe_offset < 4096:
            return False
        pe = remote.memory(addr + pe_offset, 84)
    except ValueError:
        return False
    return (pe[:6] == b"PE\0\0\x64\x86"
            and (struct.unpack_from("<I", pe, 8)[0], struct.unpack_from("<I", pe, 80)[0]) == expected)


def find_kernel(remote):
    if remote.command("Hg1") != "OK":
        raise RuntimeError("Could not select the first vCPU")
    registers = monitor("info registers")
    match = re.search(r"(?m)^IDT=\s*([0-9a-fA-F]+)\s+([0-9a-fA-F]+)", registers)
    if not match:
        return None
    idt, limit = (int(value, 16) for value in match.groups())
    if not kernel_address(idt) or limit < 15 * 16 - 1:
        return None
    for vector, rvas in IDT_HANDLERS.items():
        try:
            lo, _, _, mid, hi, _ = struct.unpack("<HHHHII", remote.memory(idt + vector * 16, 16))
        except ValueError:
            continue
        handler = lo | (mid << 16) | (hi << 32)
        for rva in rvas:
            base = handler - rva
            if kernel_address(base) and base % 4096 == 0 and image_matches(remote, base, KERNEL_IMAGE):
                return base
    return None


def find_driver(remote, head):
    link = struct.unpack("<Q", remote.memory(head + 8, 8))[0]
    for _ in range(512):
        if link == head or not kernel_address(link):
            return None
        entry = remote.memory(link, 104)
        prev = struct.unpack_from("<Q", entry, 8)[0]
        base, _, size = struct.unpack_from("<QQI", entry, 48)
        length = struct.unpack_from("<H", entry, 88)[0]
        name_ptr = struct.unpack_from("<Q", entry, 96)[0]
        if (length == 24 and kernel_address(name_ptr)
                and remote.memory(name_ptr, length).decode("utf-16-le", errors="replace").lower()
                == "nvlddmkm.sys"):
            if size != DRIVER_IMAGE[1] or not image_matches(remote, base, DRIVER_IMAGE):
                raise RuntimeError("NVIDIA driver build differs from the mapped 621.01 binary")
            return base
        link = prev
    raise RuntimeError("Windows loaded-module list exceeded the expected length")


def select_stopped_cpu(remote, reply):
    thread = re.search(r"thread:([^;]+)", reply)
    if not thread:
        return None
    if remote.command(f"Hg{thread[1]}") != "OK":
        raise RuntimeError("Could not select the vCPU that hit the breakpoint")
    return thread[1]


def step_past(remote, rip, thread):
    if thread is None:
        raise RuntimeError("QEMU did not identify the breakpoint vCPU")
    remote.breakpoint(1, rip, 1, enable=False)
    step = remote.step_guest(3, f"vCont;s:{thread}")
    if select_stopped_cpu(remote, step) != thread or stopped_registers(remote).rip == rip:
        raise RuntimeError("Could not step past the NVIDIA breakpoint")
    remote.breakpoint(1, rip, 1)


Registers = namedtuple("Registers", "rax rbx rcx rdx rsi rdi rbp rsp r8 r9 r10 r11 r12 r13 r14 r15 rip")


def stopped_registers(remote):
    data = bytes.fromhex(remote.command("g")[:17 * 16])
    return Registers(*struct.unpack_from("<17Q", data))


def read_vk(remote, driver, rax, rbx, rsp):
    caller = struct.unpack("<Q", remote.memory(rsp + 0xA8, 8))[0]
    index, source, status, size = struct.unpack("<B3xH2xII", remote.memory(rsp + 0x60, 16))
    buffer = struct.unpack("<Q", remote.memory(rsp + 0x28, 8))[0]
    returned_size = struct.unpack("<I", remote.memory(rbx, 4))[0]
    caller_rva = caller - driver
    fields = {
        "caller_rva": hex(caller_rva) if 0 <= caller_rva < DRIVER_IMAGE[1] else None,
        "phase": "full_read" if buffer else "size_query",
        "rm_status": hex(rax & 0xFFFFFFFF),
        "object_status": hex(status),
        "source": hex(source),
        "index": index,
        "size": size,
        "returned_size": returned_size,
    }
    if buffer and rax & 0xFFFFFFFF == 0 and status == 0 and returned_size == 213:
        fields["payload_sha256"] = hashlib.sha256(remote.memory(buffer, 213)).hexdigest()
    return fields


def read_key_parser_result(remote, rsp, rax):
    # The call from 0x31e706 enters with a return address, pushes two
    # registers and reserves 0xc8 bytes. Its RBP is 0x79 above its RSP.
    rbp = rsp + 0x79
    flags = struct.unpack("<I", remote.memory(rbp - 0x11, 4))[0]
    return {
        "parser_result": bool(rax & 0xFF),
        "parser_flags": hex(flags),
        "parser_state": remote.memory(rbp + 0x67, 1)[0],
    }


def read_followup(remote, rva, registers):
    if rva == VK_PARSER_RETURN:
        return read_key_parser_result(remote, registers.rsp, registers.rax)
    if rva == VK_COMPARISON_RETURN:
        return {"mismatch_mask": hex(registers.rax & 0xFFFFFFFF),
                "comparison_options": hex(registers.rdi & 0xFFFFFFFF)}
    # The words are ordered by increasing offset in each hardware record.
    expected = (registers.rsi, registers.rbp, registers.rdi, registers.r10)
    record = registers.r13
    primary = struct.unpack("<4H", remote.memory(record + 0x1A, 8))
    alternate = struct.unpack("<4H", remote.memory(record + 0x10, 8))
    return {
        "expected_words": [f"{value & 0xFFFF:04x}" for value in expected],
        "primary_words": [f"{value:04x}" for value in primary],
        "alternate_words": [f"{value:04x}" for value in alternate],
        "alternate_enabled": bool(remote.memory(record + 0x28, 1)[0]),
        "required_mask": hex(registers.rdx & 0xFFFFFFFF),
    }


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


def capture(remote, rehearsal):
    deadline = time.monotonic() + CAPTURE_SECONDS
    head = driver = None
    samples = 0
    full_reads = 0
    followup_stacks = set()
    followup_hits = 0
    detail_hits = {}
    stop = ""
    while time.monotonic() < deadline:
        if head is None:
            kernel = find_kernel(remote)
            if kernel is not None:
                head = kernel + MODULE_LIST
                log("windows_kernel_matched", base=hex(kernel))
                switch_bridge_ids()
                remote.breakpoint(2, head + 8, 8)
        thread = select_stopped_cpu(remote, stop) if head is not None and stop else None
        if head is not None and driver is None:
            try:
                driver = find_driver(remote, head)
            except ValueError:
                # Module list can be partially initialized early in boot.
                pass
            if driver is not None:
                addr = driver + LOOKUP_RETURN
                # The module list can expose the image before this code page
                # is readable. A hardware breakpoint needs only its address.
                # Verify the instructions in the stopped vCPU before reading locals.
                remote.breakpoint(1, addr, 1)
                remote.breakpoint(2, head + 8, 8, enable=False)
                log("nvidia_breakpoint_armed", base=hex(driver), address=hex(addr))
            elif rehearsal:
                log("windowed_rehearsal", module_list=hex(head), hardware_watchpoint=True,
                    nvidia_module_loaded=False)
                return
        elif driver is not None and stop:
            registers = stopped_registers(remote)
            rsp, rip = registers.rsp, registers.rip
            rva = rip - driver
            if rva == LOOKUP_RETURN:
                if remote.memory(rip, len(LOOKUP_BYTES)) != LOOKUP_BYTES:
                    raise RuntimeError("Loaded NVIDIA lookup bytes differ from the verified code")
                object_type = struct.unpack("<H", remote.memory(rsp + 0xD0, 2))[0]
                if object_type == 0x564B:
                    fields = read_vk(remote, driver, registers.rax, registers.rbx, rsp)
                    log("vk_lookup", **fields)
                    samples += 1
                    full_reads += fields["phase"] == "full_read"
                    if (fields["caller_rva"] == hex(VK_FULL_READ_CALLER)
                            and fields["phase"] == "full_read"
                            and fields["rm_status"] == fields["object_status"] == "0x0"):
                        if not followup_stacks and not followup_hits:
                            for offset in VK_FOLLOWUP:
                                remote.breakpoint(1, driver + offset, 1)
                        # Undo the callee's 0xa8-byte frame and the return address.
                        followup_stacks.add(rsp + 0xB0)
                    if samples >= MAX_SAMPLES and not followup_stacks:
                        break
                step_past(remote, rip, thread)
            elif rva in VK_FOLLOWUP:
                stage, stack_distance, instructions = VK_FOLLOWUP[rva]
                if remote.memory(rip, len(instructions)) != instructions:
                    raise RuntimeError(f"Loaded NVIDIA {stage} code differs from the verified build")
                if rsp + stack_distance in followup_stacks:
                    fields = read_followup(remote, rva, registers)
                    log("vk_followup", stage=stage, rva=hex(rva), **fields)
                    followup_hits += 1
                    if rva == VK_PARSER_RETURN:
                        break
                    detail_hits[rva] = detail_hits.get(rva, 0) + 1
                    if detail_hits[rva] >= MAX_DETAIL_HITS:
                        remote.breakpoint(1, rip, 1, enable=False)
                        stop = ""
                        continue
                step_past(remote, rip, thread)
        poll_interval = 0.16 if head is None else 2.0
        interval = min(poll_interval, max(0.05, deadline - time.monotonic()))
        stop = remote.step_guest(interval)
    log("capture_finished", kernel_matched=head is not None, driver_matched=driver is not None,
        vk_samples=samples, vk_full_reads=full_reads, vk_followup_hits=followup_hits)


def run(rehearsal):
    with tempfile.TemporaryDirectory(prefix="legion-vrr-gdb-") as directory:
        path = Path(directory)
        os.chown(path, 0, grp.getgrnam("libvirt-qemu").gr_gid)
        path.chmod(0o770)
        endpoint = path / "gdb.sock"
        remote = None
        try:
            message = monitor(f"gdbserver unix:{endpoint},server=on,wait=off")
            if not endpoint.is_socket():
                raise RuntimeError(f"QEMU did not open the private socket: {message}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.connect(str(endpoint))
                remote = Remote(conn)
                try:
                    capture(remote, rehearsal)
                finally:
                    remote.detach()
        finally:
            monitor("gdbserver none")
            if not monitor_status()["running"]:
                monitor("cont")


if __name__ == "__main__":
    if sys.argv[1:] not in ([], ["--windowed-rehearsal"]) or os.geteuid() != 0:
        raise SystemExit("Usage as root: capture-vrr-lookup.py [--windowed-rehearsal]")
    run(sys.argv[1:] == ["--windowed-rehearsal"])
