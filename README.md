# pylarnitech

Python client library for [Larnitech](https://www.larnitech.com/) smart home controllers (DE-MG, Metaforsa).

Provides async WebSocket and HTTP API access for device control and real-time status monitoring.

## Features

- WebSocket connection with real-time status push updates
- HTTP fallback for reliable request-response operations
- Automatic reconnection with exponential backoff
- AC state hex encoding/decoding (power, mode, temperature, fan, vanes)
- Blinds state hex encoding/decoding (position, tilt)
- Temperature encoding (`statusFloat2` signed 16-bit LE fixed-point)
- Admin panel API for controller discovery (serial, keys, version)
- Full type hints and `py.typed` marker

## Installation

```bash
pip install pylarnitech
```

## Quick Start

```python
import asyncio
from pylarnitech import LarnitechClient

async def main():
    client = LarnitechClient(
        host="192.168.4.100",
        api_key="7555054131",
    )

    # Get all devices
    devices = await client.get_devices()
    for dev in devices:
        print(f"{dev.addr}: {dev.type} - {dev.name}")

    # Control a lamp
    await client.set_device_status("388:3", {"state": "on"})

    # Control AC via raw hex
    await client.set_device_status_raw("407:1", "29001C620031")

    # Send IR signal
    await client.send_ir_signal("288:11", "196407000200A706...")

    # WebSocket real-time updates
    await client.connect()
    client.on_status_update(lambda data: print(f"Update: {data}"))
    # ... run event loop ...

    await client.disconnect()

asyncio.run(main())
```

## AC State Codec

```python
from pylarnitech.codec import ACState

state = ACState.from_hex("39001C620431100000")
print(state.power)        # True
print(state.mode)         # 3 (Fan only)
print(state.temperature)  # 28
print(state.fan)          # 4 (Turbo)

state.temperature = 25
state.mode = 2  # Cool
print(state.to_hex())     # "29001962..."
```

## License

MIT
