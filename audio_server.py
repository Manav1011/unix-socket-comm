import asyncio
import os
import json

SOCKET_PATH = "/tmp/audio.sock"

# Clean up old socket file if it exists
if os.path.exists(SOCKET_PATH):
    os.remove(SOCKET_PATH)

async def handle_client(reader, writer):
    print("Client connected")
    while True:
        try:
            # Read 4-byte length prefix for JSON header
            header_len_bytes = await reader.readexactly(4)
            header_len = int.from_bytes(header_len_bytes, byteorder="big")
            # Read JSON header
            header_bytes = await reader.readexactly(header_len)
            header = json.loads(header_bytes.decode())
            print(f"Received header: {header}")

            # If audio_length is present, read audio bytes
            audio_data = None
            if header.get("type") == "audio" and "audio_length" in header:
                audio_length = header["audio_length"]
                audio_data = await reader.readexactly(audio_length)
                print(f"Received audio bytes: {len(audio_data)} bytes")
                # Here you can process audio_data as needed
                response = {"type": "audio_ack", "msg": f"Received {len(audio_data)} bytes of audio"}
            elif header.get("type") == "ping":
                response = {"type": "pong", "msg": "Pong from Python!"}
            else:
                response = {"type": "error", "msg": "Unknown message type"}

            # Respond with JSON (no audio in response for now)
            response_bytes = json.dumps(response).encode()
            response_length = len(response_bytes).to_bytes(4, byteorder="big")
            writer.write(response_length + response_bytes)
            await writer.drain()

        except (asyncio.IncompleteReadError, ConnectionResetError):
            print("Client disconnected")
            break

async def main():
    server = await asyncio.start_unix_server(handle_client, path=SOCKET_PATH)
    print(f"Listening on {SOCKET_PATH}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
