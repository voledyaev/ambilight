"""One-shot diagnostic: dump USB descriptors for the ROBOBLOQ ambilight strip.

We're trying to confirm the suspected bottleneck: a 10 ms HID polling interval
that would cap throughput at ~33 FPS regardless of host software.
"""

import sys
import usb.core
import usb.util

VID, PID = 0x1A86, 0xFE07

dev = usb.core.find(idVendor=VID, idProduct=PID)
if dev is None:
    sys.exit(f"Device {VID:#06x}:{PID:#06x} not found")

print(f"Device {VID:#06x}:{PID:#06x}")
print(f"  USB version: {dev.bcdUSB:#06x}")
print(f"  Max packet size (EP0): {dev.bMaxPacketSize0}")
speed_names = {1: "LOW (1.5 Mbps)", 2: "FULL (12 Mbps)",
               3: "HIGH (480 Mbps)", 4: "SUPER (5 Gbps)"}
print(f"  Speed: {speed_names.get(dev.speed, f'unknown ({dev.speed})')}")
print(f"  Configurations: {dev.bNumConfigurations}")

for cfg in dev:
    print(f"\nConfiguration {cfg.bConfigurationValue}:")
    print(f"  Total interfaces: {cfg.bNumInterfaces}")
    for intf in cfg:
        print(f"\n  Interface {intf.bInterfaceNumber} "
              f"(alt {intf.bAlternateSetting}):")
        print(f"    Class/SubClass/Proto: "
              f"{intf.bInterfaceClass}/{intf.bInterfaceSubClass}/"
              f"{intf.bInterfaceProtocol}")
        print(f"    Endpoints: {intf.bNumEndpoints}")
        for ep in intf:
            addr = ep.bEndpointAddress
            direction = "IN" if addr & 0x80 else "OUT"
            xfer_types = {0: "CONTROL", 1: "ISOCHRONOUS",
                          2: "BULK", 3: "INTERRUPT"}
            xfer = xfer_types.get(ep.bmAttributes & 0x03, "?")
            interval_ms = ep.bInterval  # in ms for full/low speed
            print(f"      EP {addr:#04x} ({direction}, {xfer}): "
                  f"max={ep.wMaxPacketSize} B, "
                  f"bInterval={ep.bInterval} "
                  f"(~{interval_ms} ms poll)")
