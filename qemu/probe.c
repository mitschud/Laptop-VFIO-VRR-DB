typedef unsigned int u32;

static void emit(char byte)
{
    __asm__ volatile ("outb %0, %1" : : "a"(byte), "Nd"((unsigned short)0x402));
}

static u32 pci_read(u32 offset)
{
    u32 value;
    __asm__ volatile ("outl %0, %1" : : "a"(0x80000000u | offset), "Nd"((unsigned short)0xcf8));
    __asm__ volatile ("inl %1, %0" : "=a"(value) : "Nd"((unsigned short)0xcfc));
    return value;
}

static void hex(u32 value)
{
    for (int shift = 28; shift >= 0; shift -= 4) {
        emit("0123456789abcdef"[(value >> shift) & 15]);
    }
}

void efi_main(void)
{
    for (;;) {
        emit('@');
        hex(pci_read(0));
        emit(' ');
        hex(pci_read(0x2c));
        emit('\n');
        for (volatile u32 i = 0; i < 5000000; ++i) {
            __asm__ volatile ("pause");
        }
    }
}
