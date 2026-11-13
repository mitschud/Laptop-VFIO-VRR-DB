#!/usr/bin/env python3
"""Port the 82TD OEM NVPCF AML to an offline QEMU namespace."""

from pathlib import Path
import argparse
import re

HERE = Path(__file__).resolve().parent

# Minimum bytes consumed by Lenovo's CreateField instructions, including
# fields initialized before checking the subcommand.
MINIMUM_INPUT = {2: 0x17, 5: 0x10, 6: 0x0A, 7: 0x19, 9: 0x07}


def substitute_once(text, pattern, replacement):
    changed, count = re.subn(pattern, replacement, text, flags=re.S)
    if count != 1:
        raise ValueError(f"Expected one match for {pattern!r}, got {count}")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oem_ssdt", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    source = args.oem_ssdt.read_text()
    device = re.search(r"^        Device \(NPCF\)\n.*?^        \}", source, re.M | re.S)
    if device is None:
        raise ValueError("Missing Lenovo NPCF device")
    body = device[0]
    trace = (HERE / "SSDT-nvpcf-discovery.dsl").read_text().split("Method (_DSM, 4, Serialized)", 1)[1]
    trace = trace[trace.index("                VOUT = 0x43"):trace.index("                If (Arg0 !=")]
    # The handoff XML scopes this SSDT to the passed GPU configuration.
    # The host PEGP.NPCS state is absent in QEMU.
    checks = "\n".join(
        f"                If (Arg2 == {function}) {{\n"
        f"                    If (ObjectType (Arg3) != 3) {{ Return (0x80000002) }}\n"
        f"                    If (SizeOf (Arg3) < {size}) {{ Return (0x80000002) }}\n"
        "                }"
        for function, size in MINIMUM_INPUT.items()
    )
    dispatch = (
        "            {\n" + trace +
        '                If (Arg0 != ToUUID ("36b49710-2483-11e7-9598-0800200c9a66"))\n'
        "                    { Return (Buffer () { 0 }) }\n"
        "                If (Arg1 != 0x0200) { Return (0x80000001) }\n" +
        checks + "\n"
        "                Return (NPCF (Arg0, Arg1, Arg2, Arg3))\n"
        "            }\n\n"
    )
    body = substitute_once(body, r"            \{\n                If \(\(Arg0 ==.*?(?=            Method \(NPCF, 4)", dispatch)
    # A future live backend must bind both sensor values. Return an error
    # until they are available rather than reporting zero temperatures.
    sensor_branch = r"(?<=Switch \(ToInteger \(INC5\)\)\n)(.*?Case \(0x02\)\n                            \{).*?(?=                            Case \(0x03\))"
    body = substitute_once(body, sensor_branch, r"\1\n                                Return (0x80000002)\n                            }\n")
    # IOBS is zero in the cached OEM SSDT. Remove the conditional native
    # SystemIO branch entirely so no guest AML can access those I/O ports.
    body = substitute_once(
        body, r"                    Case \(0x06\)\n.*?(?=                    Case \(0x07\))",
        "                    Case (0x06) { Return (0x80000002) }\n",
    )
    # Drop only objects that lost their last use with the omitted branches.
    body = re.sub(r"^            Name \((?:LTCT|LTGT|LTIN|IOBS|CMPL|CNPL|HPCT), [^\n]+\n", "", body, flags=re.M)
    if any(token in body for token in ("OperationRegion", "PC00.", "EC0.")):
        raise ValueError("The port still contains a physical firmware dependency")
    target = args.output
    target.write_text(
        '// Offline OEM port, pending validation against the Windows driver.\n'
        'DefinitionBlock ("", "SSDT", 2, "LNVVM", "NPCFPORT", 1)\n'
        '{\n'
        '    External (\\_SB.VHEX, MethodObj)\n'
        '    External (\\_SB.VOUT, FieldUnitObj)\n'
        '    Scope (\\_SB)\n'
        '    {\n' + body + '\n    }\n}\n'
    )


if __name__ == "__main__":
    main()
