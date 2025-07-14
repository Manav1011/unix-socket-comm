import asyncio
import os
import struct

SOCKET_PATH = "/tmp/audio.sock"

# Clean up old socket file if it exists
if os.path.exists(SOCKET_PATH):
    os.remove(SOCKET_PATH)

async def handle_client(reader, writer):
    print("Client connected")
    while True:
        try:
            # Read 4-byte length prefix
            length_bytes = await reader.readexactly(4)
            length = struct.unpack(">I", length_bytes)[0]

            # Read audio data
            audio_data = await reader.readexactly(length)

            # Log info or process here
            print(f"Received {length} bytes of audio data")

            # Optional: Send back confirmation
            writer.write(b"OK\n")
            await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            print("Client disconnected")
            break

async def main():
    server = await asyncio.start_unix_server(handle_client, path=SOCKET_PATH)
    print(f"Listening on {SOCKET_PATH}")
    async with server:
        await server.serve_forever()

asyncio.run(main())
