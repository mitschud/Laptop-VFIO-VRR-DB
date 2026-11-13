            OperationRegion (NPCU, SystemIO, 0x5E0, 8)
            Field (NPCU, ByteAcc, NoLock, Preserve)
            {
                UDAT, 8, UIER, 8, UFCR, 8, ULCR, 8, UMCR, 8, ULSR, 8
            }
            Name (PSEN, Zero)
            Name (SEQN, Zero)
            Name (CTMP, Zero)
            Name (GTMP, Zero)
            Mutex (TMTX, 0)

            Method (USND, 1, NotSerialized)
            {
                Local0 = Timer
                While (!(ULSR & 0x20))
                {
                    If ((Timer - Local0) >= 50000) { Return (Zero) }
                    Stall (50)
                }
                UDAT = Arg0
                Return (One)
            }

            Method (SNAP, 0, Serialized)
            {
                // Configure the reserved UART for 115200 baud, 8N1.
                ULCR = 0x83
                UDAT = One
                UIER = Zero
                ULCR = 0x03
                UIER = Zero
                UFCR = 0x07
                SEQN = ((SEQN + One) & 0xFF)
                If (!USND (0xA6)) { Return (Zero) }
                If (!USND (SEQN)) { Return (Zero) }
                Local0 = Buffer (16) {}
                Local1 = Timer
                Local2 = Zero
                Local3 = Zero
                While (Local2 < SizeOf (Local0))
                {
                    If ((Timer - Local1) >= 1500000)
                    {
                        Return (Zero)
                    }
                    If (ULSR & One)
                    {
                        Local4 = UDAT
                        Index (Local0, Local2) = Local4
                        Local3 += Local4
                        Increment (Local2)
                    }
                    Else
                    {
                        Sleep (One)
                    }
                }
                If ((DerefOf (Index (Local0, Zero)) != 0xA5) ||
                    (DerefOf (Index (Local0, One)) != SEQN) || (Local3 & 0xFF))
                {
                    Return (Zero)
                }
                Return (Local0)
            }

            Method (ACST, 0, Serialized)
            {
                Local0 = SNAP ()
                If (ObjectType (Local0) != 3) { Return (Zero) }
                Return ((DerefOf (Index (Local0, 2)) >> 2) & One)
            }

            Method (UPDT, 1, Serialized)
            {
                Local0 = SNAP ()
                If (ObjectType (Local0) != 3) { Return (Zero) }
                CreateByteField (Local0, 2, FLAG)
                // A battery/unknown frame cannot supply the AC-mode policy.
                If (!(FLAG & 4)) { Return (Zero) }
                If (Arg0 == 2)
                {
                    If (!(FLAG & One)) { Return (Zero) }
                    CreateWordField (Local0, 3, CTGP)
                    CreateWordField (Local0, 5, TPPW)
                    CreateWordField (Local0, 7, PPAB)
                    CreateWordField (Local0, 9, MINW)
                    CreateByteField (Local0, 11, ACDB)
                    CreateByteField (Local0, 12, DCDB)
                    Local1 = (ACBT != CTGP) || (ATPP != TPPW) ||
                             (AMAT != PPAB) || (AMIT != MINW) ||
                             (DBAC != ACDB) || (DBDC != DCDB)
                    ACBT = CTGP
                    ATPP = TPPW
                    AMAT = PPAB
                    AMIT = MINW
                    DBAC = ACDB
                    DBDC = DCDB
                    If (PSEN && Local1) { Notify (\_SB.NPCF, 0xC0) }
                    PSEN = One
                }
                Else
                {
                    If (!(FLAG & 2)) { Return (Zero) }
                    CTMP = DerefOf (Index (Local0, 13))
                    GTMP = DerefOf (Index (Local0, 14))
                }
                Return (One)
            }
