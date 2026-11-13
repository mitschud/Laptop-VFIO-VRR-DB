# Dynamic Boost and G-SYNC in a laptop VFIO guest

A working configuration for a Windows KVM guest driving the laptop's physical internal display through a passed-through NVIDIA GPU. The reference machine is a **Lenovo Legion 7i Gen 7 (16IAX7, 82TD)** with BIOS **K1CN40WW**, an **RTX 3080 Ti Laptop GPU**, and a **BOE0A1F 2560×1600 165 Hz eDP panel**. The host ran CachyOS with QEMU 11.1.3 and libvirt. The guest ran Windows 11 build 26100.9444 with NVIDIA 621.01.

| Feature | Change that made it available | Observed result |
| --- | --- | --- |
| Dynamic Boost | Expose the laptop's ACPI NVPCF interface in the guest and feed it live host data over an emulated serial link. | NVIDIA Control Panel reported **Dynamic Boost: Yes**. Wattage redistribution under a controlled load remains unmeasured. |
| G-SYNC on the internal eDP panel | Deliver the original panel EDID and OEM NVIDIA display responses in guest ACPI, then switch the emulated Q35 host bridge's PCI IDs at Windows kernel startup. | NVIDIA's firmware key check passed, **Set up G-SYNC** appeared, and NVAPI reported VRR supported, capable and possible. The recorded desktop sample had VRR active/enabled false. Verify active refresh during a game. |

The live NVPCF bridge alone enabled Dynamic Boost on this machine. G-SYNC stayed unavailable until the late Q35 switch. Before the switch, the guest already reported the same full EDID as Linux, internal eDP, a working NVIDIA driver and AC power. Windows Settings still reported VRR unsupported after NVIDIA exposed G-SYNC. Use NVIDIA Control Panel and runtime checks for this setup.

## Prerequisites

- Start with a working libvirt **Q35 + OVMF** guest with VFIO passthrough and a physical panel route that the Windows NVIDIA driver sees as eDP. Confirm VRR works on the panel on the host or native Windows.
- Save the original domain XML and keep a tested route back to Linux or a windowed guest. Use a stopped guest when editing its definition. For the first test, cold-boot the guest from the host.
- Plug into AC for the supplied NVPCF broker. You need Python 3, ACPICA tools (`acpidump`, `acpixtract`, `iasl`), QEMU build dependencies and Linux's `acpi_call` module.
- The included ACPI generators, host broker and boot worker are specific to the tested firmware and guest build. Other machines need their own ACPI namespace, firmware responses and bridge IDs. A Windows update can change the kernel signature used to time the switch.

## 1. Generate the guest ACPI tables

Dump and disassemble your laptop's ACPI tables on Linux. Identify the SSDT containing both `Device (NPCF)` and `Method (NBCI, 4, Serialized)` by inspecting the disassembly. The table number can vary. Use your own original internal-panel EDID from the relevant `/sys/class/drm/card*-eDP-*/edid` path. The generators use the 82TD table structure and require the OEM buffer names shown in the source.

For example, collect and inspect the tables in a private working directory:

```sh
mkdir -p ~/legion-acpi
cd ~/legion-acpi
sudo acpidump > host-acpi.txt
acpixtract -a host-acpi.txt
iasl -da dsdt.dat ssdt*.dat
rg -l 'Device \(NPCF\)' ssdt*.dsl
rg -l 'Method \(NBCI,' ssdt*.dsl
```

Choose the file listed by both searches. Keep the firmware dump and generated tables local.

From this repository, set `OEM` and `EDID` to those two absolute paths, then run:

```sh
mkdir -p build
python3 acpi/port-nvpcf.py "$OEM" build/nvpcf-port.dsl
python3 acpi/add-live-inputs.py "$OEM" build/nvpcf-port.dsl build/nvpcf.dsl
python3 acpi/build-display.py "$OEM" build/display.dsl
iasl -p build/nvpcf build/nvpcf.dsl
iasl -p build/display build/display.dsl
python3 acpi/make-ddc-ssdt.py "$EDID" --out-prefix build/ddc
```

The display table exposes the AC adapter and the Lenovo/NVIDIA display objects from the OEM firmware. The DDC table exposes the panel's EDID in complete 128-byte blocks and validates every block checksum. Both tables target `\_SB.PCI0.S15.S00` on the tested guest. Verify the GPU's ACPI scope in your own guest DSDT before loading them. Add only these adapted SSDTs to the Q35 guest.

## 2. Wire the live NVPCF bridge into the guest

Install the three compiled AML files where libvirt's QEMU account can read them:

```sh
sudo install -d -m 0755 /usr/local/share/legion-vfio
sudo install -m 0644 build/display.aml build/nvpcf.aml build/ddc.aml /usr/local/share/legion-vfio/
```

Merge these elements into your existing domain XML. Keep existing required ACPI tables such as the battery table. Remove older experimental tables that define the same objects. Use the libvirt QEMU XML namespace on the root element when adding `qemu:commandline`.

```xml
<!-- Inside <os>, merge into the existing <acpi> element. -->
<acpi>
  <table type='raw'>/usr/local/share/legion-vfio/display.aml</table>
  <table type='raw'>/usr/local/share/legion-vfio/nvpcf.aml</table>
  <table type='raw'>/usr/local/share/legion-vfio/ddc.aml</table>
</acpi>
```

```xml
<!-- Inside <domain xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>. -->
<qemu:commandline>
  <qemu:arg value='-chardev'/>
  <qemu:arg value='socket,id=npcf,path=/run/legion-nvpcf/uart,server=off'/>
  <qemu:arg value='-device'/>
  <qemu:arg value='isa-serial,chardev=npcf,iobase=0x5e0,irq=3'/>
  <qemu:arg value='-chardev'/>
  <qemu:arg value='file,id=legionvrr,path=/var/log/libvirt/qemu/legion-vrr-probe.log'/>
  <qemu:arg value='-device'/>
  <qemu:arg value='isa-debugcon,iobase=0x505,chardev=legionvrr'/>
</qemu:commandline>
```

Use a root-owned broker script. For the first test, start it in a dedicated terminal or system service and wait for the `ready` file beside the socket before starting the guest:

```sh
sudo modprobe acpi_call
sudo install -D -m 0755 acpi/broker.py /usr/local/libexec/legion-nvpcf-broker
sudo install -d -o root -g libvirt-qemu -m 0750 /run/legion-nvpcf
sudo /usr/local/libexec/legion-nvpcf-broker /run/legion-nvpcf/uart --listen
```

The example uses `libvirt-qemu` as the QEMU group. Check your distribution's QEMU user, group and security policy, and reserve both I/O ports in the guest. The broker accepts one VM connection and needs to be started again before the next launch. Automate that lifecycle in the host handoff service once it works manually.

The guest supplies a sequence byte. The broker returns a bounded snapshot of AC state, six allowlisted OEM fields and two temperature readings. Windows gets no arbitrary host ACPI or EC access. In Windows, check `ACPI\NVDA0820\NPCF` in Device Manager and **Dynamic Boost** in NVIDIA Control Panel → System Information. A stale root-enumerated controller can coexist with the working ACPI instance. Host power policy changes are detected on a guest poll. Independent idle notifications remain unverified.

## 3. Build QEMU with the late PCI identity switch

Apply the [patch](qemu/qemu-11.1.3-legion-late-bridge.patch) to [QEMU 11.1.3](https://download.qemu.org/qemu-11.1.3.tar.xz). From the extracted QEMU source, with `GUIDE` set to this repository's absolute path:

```sh
patch -p1 < "$GUIDE/qemu/qemu-11.1.3-legion-late-bridge.patch"
mkdir -p build
cd build
../configure --prefix=/usr --sysconfdir=/etc --libexecdir=/usr/lib/qemu \
  --target-list=x86_64-softmmu --disable-download --disable-docs --disable-werror \
  --enable-kvm --enable-tpm --enable-slirp --enable-libusb --enable-pipewire --disable-modules
ninja qemu-system-x86_64
sudo install -m 0755 qemu-system-x86_64 /usr/local/libexec/qemu-system-x86_64-legion-q35-late
```

Select the separate executable in `<devices><emulator>` for the GPU handoff. Add `-L` and the system QEMU ROM directory (on the tested host, `/usr/share/qemu`) as two entries in the existing `qemu:commandline`. The host keeps its package-managed QEMU executable for ordinary launches.

The physical Legion bridge at PCI `00:00.0` reports `8086:4637`, subsystem `17aa:3801`. Stock Q35 reports `8086:29c0`, subsystem `1af4:1100`. NVIDIA 621.01 compared all four ID words during its OEM firmware key check and rejected the stock bridge. With the late switch it accepted the key. The patch changes only the device and subsystem identity words and preserves Q35 register emulation. Keep the Q35 IDs through OVMF and on reset. Changing them at power-on prevented the earlier test from reaching the EFI probe.

## 4. Start paused and run the host boot worker

With the NVPCF broker ready and the patched emulator selected for the GPU guest, start it paused. Set `VM` to your libvirt guest name. Run these from a terminal or service that remains available through the GPU handoff:

```sh
sudo virsh -c qemu:///system start "$VM" --paused
sudo env LEGION_VFIO_VM="$VM" python3 "$GUIDE/qemu/capture-late-bridge.py"
```

Run the worker immediately after starting paused. It uses a private host debugger socket to identify the Windows kernel, calls the patch's `qom-set` property `mch.x-legion-host-bridge-ids`, and traces the NVIDIA firmware key result for the tested driver. The worker closes the debugger and resumes the guest on exit. If it is interrupted and the guest remains paused, close the debugger with `virsh qemu-monitor-command "$VM" --hmp 'gdbserver none'` on the system connection, then use `virsh resume "$VM"`. In a single-GPU setup, put the paused start, worker and resume cleanup in the existing host handoff service. The guest PCI IDs reset on reboot, so the tested launcher reapplies the switch on each cold start.

Look for `legion_bridge_ids_enabled` followed by `key_parser_return` with `parser_result: true`. In Windows, open **NVIDIA Control Panel → Display → Set up G-SYNC**, select the internal panel and apply. Enable the G-SYNC indicator and test a variable-frame-rate workload within the panel's VRR range. Check the panel refresh if you have a way to measure it. Use the indicator or measurement to confirm active VRR. Capability flags alone establish eligibility.

## Troubleshooting and rollback

- **NVPCF enumerates but Dynamic Boost stays unavailable.** Check that the broker reports AC and budgets available, that QEMU connected to its socket, and that the ACPI instance has a working driver. Check the guest AC adapter's online status.
- **Windows boots but G-SYNC is missing.** Check for the bridge-switch event and successful key parse. Confirm that the passed GPU reports internal eDP and that Windows reads the full original EDID. Inspect the guest DSDT path if the OEM methods never run.
- **Windows update or guest restart.** The supplied worker is tied to the tested Windows kernel, and parser instrumentation is tied to NVIDIA 621.01. Revalidate those checkpoints before relying on the patch with another build. Use a cold VM start to run the host worker again.
- **Return to stock.** Shut down the guest, restore the saved XML and original QEMU emulator, and stop the NVPCF broker. The stock emulator remains installed.

The patch and helper sources are included. Generate OEM firmware-derived tables and EDID locally. This guide covers an already working VFIO VM and has been validated on the listed Legion configuration.
