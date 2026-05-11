"""Open the device and dump enumeration info — no writes."""

import hid

from ambilight.device import PRODUCT_ID, VENDOR_ID, AmbilightDevice

print(f"Looking for HID interfaces of {VENDOR_ID:#06x}:{PRODUCT_ID:#06x}...")
for d in hid.enumerate(VENDOR_ID, PRODUCT_ID):
    print(
        f"  iface={d.get('interface_number'):>2}  "
        f"usage={d.get('usage'):#06x}/{d.get('usage_page'):#06x}  "
        f"product={d.get('product_string')!r}  "
        f"path={d.get('path')!r}"
    )

print("\nOpening AmbilightDevice (interface 0)...")
with AmbilightDevice() as dev:
    info = dev._dev.get_manufacturer_string()
    product = dev._dev.get_product_string()
    print(f"  ✓ opened, manufacturer={info!r}, product={product!r}")
print("Closed cleanly.")
