DefinitionBlock ("", "SSDT", 2, "LNVVM", "NPCFTEST", 1)
{
    External (\_SB.VHEX, MethodObj)
    External (\_SB.VOUT, FieldUnitObj)
    Scope (\_SB)
    {
        Device (NPCF)
        {
            Name (_HID, "NVDA0820")
            Name (_UID, "NPCF")
            Name (CDIS, Zero)
            Method (_STA, 0) { If (CDIS) { Return (0x0D) } Return (0x0F) }
            Method (_DIS, 0) { CDIS = One }
            Method (_INI, 0) { VOUT = 0x4E; VOUT = 0x0A }
            // Explicitly zero autonomous power controllers; never advertise a
            // controller whose live sensors or host power operations we cannot supply.
            Name (STBL, Buffer () {
                0x20, 0x03, 0x01, 0x00,
                0x21, 0x05, 0x02, 0x0F, 0x00, 0x64, 0x00, 0x41
            })
            Name (DTBL, Buffer () {
                0x21, 0x05, 0x10, 0x1C, 0x00,
                0,0,0,0, 0,0,0,0, 0,0,0,0, 0,0,0,0
            })
            Method (_DSM, 4, Serialized)
            {
                VOUT = 0x43
                VHEX (Arg1); VOUT = 0x20; VHEX (Arg2)
                VOUT = 0x20; VHEX (ObjectType (Arg3))
                If (ObjectType (Arg3) == 3) {
                    VOUT = 0x20; VHEX (SizeOf (Arg3))
                    Local0 = Zero
                    While ((Local0 < SizeOf (Arg3)) && (Local0 < 16)) {
                        VOUT = 0x20; VHEX (DerefOf (Index (Arg3, Local0)))
                        Increment (Local0)
                    }
                }
                VOUT = 0x0A
                If (Arg0 != ToUUID ("36b49710-2483-11e7-9598-0800200c9a66")) {
                    Return (Buffer () {0})
                }
                If (Arg1 != 0x0200) { Return (0x80000001) }
                Switch (ToInteger (Arg2)) {
                    Case (Zero) { Return (Buffer () {0x07,0,0,0}) }
                    Case (One) { Return (STBL) }
                    Case (2) {
                        If (ObjectType (Arg3) != 3) { Return (0x80000002) }
                        If (SizeOf (Arg3) < 0x15) { Return (0x80000002) }
                        // Header must be v2.1,5,16,28,0 and command must be GET.
                        If (DerefOf (Index (Arg3, Zero)) != 0x21) { Return (0x80000002) }
                        If (DerefOf (Index (Arg3, One)) != 5) { Return (0x80000002) }
                        If (DerefOf (Index (Arg3, 2)) != 0x10) { Return (0x80000002) }
                        If (DerefOf (Index (Arg3, 3)) != 0x1C) { Return (0x80000002) }
                        If (DerefOf (Index (Arg3, 4)) != Zero) { Return (0x80000002) }
                        If ((DerefOf (Index (Arg3, 5)) & 3) != Zero) { Return (0x80000002) }
                        Return (DTBL)
                    }
                }
                VOUT = 0x58; VOUT = 0x0A
                Return (0x80000002)
            }
        }
    }
}
