#!/usr/bin/env python3
"""Attach live snapshot inputs to the tested OEM NVPCF port."""

from pathlib import Path
import argparse
import re

HERE = Path(__file__).resolve().parent
TEMPERATURE_BLOCK = (
    r"(?<=Switch \(ToInteger \(INC5\)\)\n).*?"
    r"(                            Case \(0x02\)\n                            \{\n.*?)"
    r"(?=                            Case \(0x03\))"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oem_ssdt", type=Path)
    parser.add_argument("nvpcf_port", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    text = args.nvpcf_port.read_text().replace('"NPCFPORT"', '"NPCFLIVE"')
    anchor = "            Method (_DSM, 4, Serialized)"
    assert text.count(anchor) == 1
    text = text.replace(anchor, (HERE / "uart.asl").read_text() + "\n" + anchor)
    native = re.search(TEMPERATURE_BLOCK, args.oem_ssdt.read_text(), re.S)
    assert native is not None
    temperature = native[1]
    for old, new in (("ECMT", "TMTX"), ("CPUT", "CTMP"), ("GPDT", "GTMP")):
        temperature = temperature.replace(r"\_SB.PC00.LPCB.EC0." + old, new)
    start = "                            Case (0x02)\n                            {\n"
    assert temperature.startswith(start)
    temperature = start + "                                If (!UPDT (5)) { Return (0x80000002) }\n" + temperature[len(start):]
    pattern = r"(Switch \(ToInteger \(INC5\)\)\n.*?)(                            Case \(0x02\)\n.*?)(?=                            Case \(0x03\))"
    text, count = re.subn(pattern, lambda match: match[1] + temperature, text, flags=re.S)
    assert count == 1
    fields = "            Name (LTCT, Zero)\n            Name (LTGT, Zero)\n            Name (LTIN, Zero)\n"
    text = text.replace(anchor, fields + anchor)
    power = "                    Case (0x02)\n                    {\n"
    assert text.count(power) == 1
    text = text.replace(power, power + "                        If (!UPDT (2)) { Return (0x80000002) }\n")
    assert "EC0." not in text
    args.output.write_text(text)


if __name__ == "__main__":
    main()
