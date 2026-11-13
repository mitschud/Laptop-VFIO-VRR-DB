#!/usr/bin/env python3
"""Generate the 82TD display SSDT from the owner's disassembled OEM table."""
import argparse
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent


def values(body, count, maximum):
    body = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.S)
    words = [word.strip() for word in body.split(",")]
    constants = {"Zero": "0", "One": "1"}
    numbers = [int(constants.get(word, word), 16) for word in words]
    if len(numbers) != count or any(value > maximum for value in numbers):
        raise ValueError("Unexpected OEM response size or value")
    return ", ".join(f"0x{value:02X}" for value in numbers)


def extract(pattern, text):
    match = re.search(pattern, text)
    if match is None:
        raise ValueError("Expected 82TD ACPI object missing")
    return match[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oem_ssdt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.oem_ssdt.read_text()
    template = (HERE / "display.asl.in").read_text()
    for name, count in (("GSV2", 0xD5), ("GDR1", 0xDF)):
        body = extract(rf"Name \({name}, Buffer \(0x[0-9A-Fa-f]+\)\s*\{{([^}}]+)\}}", source)
        template = template.replace(f"@{name}@", values(body, count, 0xFF))
    nbci = source.split("Method (NBCI, 4, Serialized)", 1)[1]
    mask = extract(r"Return \(Buffer \(0x04\)\s*\{([^}]+)\}\)", nbci.split("NBCI fun0 NBCI_FUNC_SUPPORT", 1)[1])
    backlight = extract(r"Return \(Package \(0x20\)\s*\{([^}]+)\}\)", nbci.split("NBCI fun20 NBCI_FUNC_GETBACKLIGHT", 1)[1])
    if not values(backlight, 32, 0xFFFFFFFF).startswith("0x8000A450,"):
        raise ValueError("Unexpected internal panel address")
    template = template.replace("@NBCI_MASK@", values(mask, 4, 0xFF))
    template = template.replace("@NBCI_BACKLIGHT@", values(backlight, 32, 0xFFFFFFFF))
    args.output.write_text(template)


if __name__ == "__main__":
    main()
