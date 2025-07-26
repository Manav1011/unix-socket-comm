import asyncio
from inspect import trace
import os
import json
import sys
import datetime
from collections import defaultdict
import io
import traceback
import numpy as np
import soundfile as sf
import warnings
from pathlib import Path
import logging

# ---------------------------------------------------------------------------
# Django setup (same as in the former socket_io_client.py)
# ---------------------------------------------------------------------------
# Get absolute path to Django project root (the outer SMARTROLL directory)
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
DJANGO_ROOT = str(Path(PROJECT_ROOT) / "SMARTROLL")  # Inner SMARTROLL directory
sys.path.insert(0, PROJECT_ROOT)  # Add project root to Python path
sys.path.insert(0, DJANGO_ROOT)   # Add Django project root to Python path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "SMARTROLL.settings")
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
os.environ['FROM_SOCKET_SCRIPT'] = 'true'

import django  # noqa: E402  (import after env-vars)
django.setup()

# Django / project imports (after setup)
import jwt  # noqa: E402
from channels.db import database_sync_to_async  # noqa: E402
from django.core.files.storage import default_storage  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402

from Session.models import Session, Attendance, AudioEmbeddings, FeatureVectors  # noqa: E402
from StakeHolders.models import Teacher  # noqa: E402
from Session.serializers import (
    AttendanceSerializer,
    SessionSerializerHistoryPresent,
    SessionSerializerHistory,
)  # noqa: E402
from SMARTROLL.GlobalUtils import extract_yamnet_embedding, calculate_features_extra  # noqa: E402

# ---------------------------------------------------------------------------
# Config & globals
# ---------------------------------------------------------------------------
SOCKET_PATH = "/tmp/audio.sock"

SAMPLE_RATE = 48000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH

audio_buffers = defaultdict(lambda: {"buffer": bytearray(), "count": 0, "start_time": None})

warnings.filterwarnings(
    "ignore",
    category=RuntimeWarning,
    message="DateTimeField .* received a naive datetime",
)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("audio_server")

# Clean up old socket file if it exists
if os.path.exists(SOCKET_PATH):
    logger.info(f"Removing old socket file at {SOCKET_PATH}")
    os.remove(SOCKET_PATH)

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def wrap_pcm_to_wav(pcm_bytes: bytes, samplerate: int = SAMPLE_RATE, channels: int = CHANNELS):
    logger.debug("Wrapping PCM bytes to WAV format.")
    wav_io = io.BytesIO()
    audio_array = np.frombuffer(pcm_bytes, dtype=np.int16)
    sf.write(wav_io, audio_array, samplerate, format="WAV", subtype="PCM_16")
    wav_io.seek(0)
    return wav_io


async def send_response(writer: asyncio.StreamWriter, response_dict: dict, audio_bytes: bytes | None = None):
    logger.info(f"Sending response: {response_dict.get('type', 'unknown')} | Status: {response_dict.get('status_code', '')}")
    header_bytes = json.dumps(response_dict).encode()
    writer.write(len(header_bytes).to_bytes(4, "big") + header_bytes)
    if audio_bytes:
        logger.debug("Sending audio bytes with response.")
        writer.write(audio_bytes)
    await writer.drain()


async def process_incoming_audio(session_id: str, auth_token: str, start_time_ms: int, audio_chunk: bytes, writer: asyncio.StreamWriter):
    try:
        logger.info(f"Received audio chunk for session_id={session_id}")
        buf = audio_buffers[session_id]
        # Set start time for the very first chunk
        if buf["count"] == 0:
            buf["start_time"] = datetime.datetime.fromtimestamp(start_time_ms / 1000)
            logger.debug(f"Set start_time for session_id={session_id} to {buf['start_time']}")

        buf["buffer"].extend(audio_chunk)
        buf["count"] += 1
        logger.debug(f"Audio buffer count for session_id={session_id}: {buf['count']}")

        if buf["count"] == 5:
            logger.info(f"Collected 5 audio chunks for session_id={session_id}, processing...")
            full_audio = bytes(buf["buffer"])
            wav_io = wrap_pcm_to_wav(full_audio)
            # default_storage.save('attendances/TEST_CLASS_LATEST/teacher.wav', ContentFile(wav_io.read()))
            wav_io.seek(0)
            db_resp = await save_incoming_audio_chunks(
                session_id,
                auth_token,
                buf["start_time"],
                wav_io.read(),
            )

            # Reset buffer
            buf["buffer"].clear()
            buf["count"] = 0
            buf["start_time"] = None
            logger.info(f"Audio buffer reset for session_id={session_id}")

            await send_response(
                writer,
                {"type": "incoming_audio_chunks", "status_code": 200 if db_resp["status"] else 500, "data": db_resp},
            )
    except Exception as e:
        print(e)
        traceback.format_exc()
# ---------------------------------------------------------------------------
# Database helpers (ported from socket_io_client.py)
# ---------------------------------------------------------------------------

@database_sync_to_async
def authenticate_user(session_id, auth_token):
    try:
        session_obj = Session.objects.filter(session_id=session_id).first()
        if session_obj:
            decoded = jwt.decode(auth_token, options={"verify_signature": False})
            if decoded["obj"]["profile"]["role"] == "teacher":
                teacher = Teacher.objects.filter(slug=decoded["obj"]["slug"]).first()
                if session_obj.lecture.teacher == teacher:
                    return {"session_id": session_id, "auth_token": auth_token, "status": True, "message": "Authenticated"}
                return {"session_id": session_id, "status": False, "message": "Authentication rejected"}
            return {"session_id": session_id, "status": False, "message": "Permission denied"}
        return {"session_id": session_id, "status": False, "message": "Session not found"}
    except jwt.exceptions.DecodeError:
        return {"session_id": session_id, "status": False, "message": "Invalid auth token"}
    except Exception as e:
        return {"session_id": session_id, "status": False, "message": str(e)}


@database_sync_to_async
def update_attendance_func(session_id, attendance_slug, auth_token, action):
    try:
        session_obj = Session.objects.filter(session_id=session_id).first()
        if session_obj:
            decoded = jwt.decode(auth_token, options={"verify_signature": False})
            if decoded["obj"]["profile"]["role"] == "teacher":
                attendance_obj = Attendance.objects.filter(slug=attendance_slug).first()
                if not attendance_obj or not session_obj.attendances.contains(attendance_obj):
                    return {"status": False, "message": "Attendance object invalid"}
                attendance_obj.is_present = action
                attendance_obj.manual = True
                attendance_obj.marking_time = datetime.datetime.now()
                attendance_obj.save()
                return {"status": True, "message": "Attendance updated"}
            return {"status": False, "message": "Permission denied"}
        return {"status": False, "message": "Session not found"}
    except Exception as e:
        return {"status": False, "message": str(e)}


@database_sync_to_async
def regulization_request_entry(session_id, attendance_slugs, auth_token):
    try:
        session_obj = Session.objects.filter(session_id=session_id).first()
        if not session_obj:
            return {"status": False, "message": "Session not found"}
        decoded = jwt.decode(auth_token, options={"verify_signature": False})
        if decoded["obj"]["profile"]["role"] != "teacher":
            return {"status": False, "message": "Permission denied"}
        teacher = Teacher.objects.filter(slug=decoded["obj"]["slug"]).first()
        if session_obj.lecture.teacher != teacher:
            return {"status": False, "message": "Permission denied"}
        successful = []
        for slug in attendance_slugs:
            att = Attendance.objects.filter(slug=slug).first()
            if not att or not session_obj.attendances.contains(att) or att.is_present:
                continue
            att.is_present = True
            att.manual = True
            att.marking_time = datetime.datetime.now()
            att.save()
            successful.append(att)
        serialized = AttendanceSerializer(successful, many=True).data
        return {"status": True, "data": serialized, "message": "Regulization accepted"}
    except Exception as e:
        return {"status": False, "message": str(e)}


@database_sync_to_async
def get_ongoing_session_data(session_id, auth_token):
    try:
        session_obj = Session.objects.filter(session_id=session_id).first()
        if not session_obj or session_obj.active != "ongoing":
            return {"status": False, "session_id":session_id, "message": "Session inactive"}
        decoded = jwt.decode(auth_token, options={"verify_signature": False})
        if decoded["obj"]["profile"]["role"] != "teacher":
            return {"status": False,"session_id":session_id, "message": "Permission denied"}
        teacher = Teacher.objects.filter(slug=decoded["obj"]["slug"]).first()
        if session_obj.lecture.teacher != teacher:
            return {"status": False,"session_id":session_id, "message": "Permission denied"}
        data = SessionSerializerHistoryPresent(session_obj).data
        return {"status": True, "data": data,"session_id":session_id}
    except Exception as e:        
        traceback.print_exc()
        return {"status": False, "message": str(e)}


@database_sync_to_async
def save_incoming_audio_chunks(session_id, auth_token, start_time, wav_bytes):
    try:
        session_obj = Session.objects.filter(session_id=session_id).first()
        if not session_obj or session_obj.active != "ongoing":
            return {"status": False, "message": "Session inactive"}
        decoded = jwt.decode(auth_token, options={"verify_signature": False})
        if decoded["obj"]["profile"]["role"] != "teacher":
            return {"status": False, "message": "Permission denied"}
        teacher = Teacher.objects.filter(slug=decoded["obj"]["slug"]).first()
        if session_obj.lecture.teacher != teacher:
            return {"status": False, "message": "Permission denied"}
        embeddings = extract_yamnet_embedding(wav_bytes)
        features = calculate_features_extra(wav_bytes)
        emb_obj = AudioEmbeddings.objects.create(profile=teacher.profile, start_time=start_time, vector=embeddings)
        feat_obj = FeatureVectors.objects.create(profile=teacher.profile, start_time=start_time, vector=features)
        session_obj.embeddings.add(emb_obj)
        session_obj.features.add(feat_obj)
        return {"status": True, "message": "Audio processed"}
    except Exception as e:
        traceback.print_exc()
        return {"status": False, "message": str(e)}


@database_sync_to_async
def end_session(session_id, auth_token):
    try:
        session_obj = Session.objects.filter(session_id=session_id).first()
        if not session_obj or session_obj.active != "ongoing":
            return {"status": False, "message": "Session inactive"}
        decoded = jwt.decode(auth_token, options={"verify_signature": False})
        if decoded["obj"]["profile"]["role"] != "teacher":
            return {"status": False, "message": "Permission denied"}
        teacher = Teacher.objects.filter(slug=decoded["obj"]["slug"]).first()
        if session_obj.lecture.teacher != teacher:
            return {"status": False, "message": "Permission denied"}
        session_obj.active = "post"
        if session_obj.lecture.is_proxy:
            session_obj.lecture.is_active = False
            session_obj.lecture.save()
        session_obj.save()
        data = SessionSerializerHistory(session_obj).data
        return {"status": True, "data": data, "message": "Session ended"}
    except Exception as e:
        return {"status": False, "message": str(e)}

# ---------------------------------------------------------------------------
# Socket client handler
# ---------------------------------------------------------------------------

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    logger.info(f"Client {addr} connected")
    try:
        while True:
            # 1) Header length (4 bytes, big-endian)
            header_len_bytes = await reader.readexactly(4)
            header_len = int.from_bytes(header_len_bytes, "big")

            # 2) Header JSON
            header_json = await reader.readexactly(header_len)
            header = json.loads(header_json.decode())
            msg_type = header.get("type")
            logger.info(f"Received message type: {msg_type} from client {addr}")

            # ------------------------------------------------------------------
            # Route per message type
            # ------------------------------------------------------------------
            if msg_type in ("ping", "healthcheck"):
                logger.debug("Received ping/healthcheck event.")
                # Respond with a generic pong so clients can perform heart-beat checks
                await send_response(writer, {"type": "pong", "msg": "Pong from Python!", "status_code": 200})

            elif msg_type == "authentication":
                logger.info(f"Authenticating session_id={header.get('session_id')}")
                db_resp = await authenticate_user(header.get("session_id"), header.get("auth_token"))
                await send_response(writer, {"type": "authentication", "status_code": 200 if db_resp["status"] else 500, "data": db_resp})

            elif msg_type == "update_attendance":
                logger.info(f"Updating attendance for session_id={header.get('session_id')}, attendance_slug={header.get('attendance_slug')}")
                db_resp = await update_attendance_func(
                    header.get("session_id"),
                    header.get("attendance_slug"),
                    header.get("auth_token"),
                    header.get("action"),
                )
                await send_response(writer, {"type": "update_attendance", "status_code": 200 if db_resp["status"] else 500, "data": db_resp})

            elif msg_type == "regulization_request":
                logger.info(f"Regulization request for session_id={header.get('session_id')}")
                db_resp = await regulization_request_entry(
                    header.get("session_id"),
                    header.get("attendance_slugs", []),
                    header.get("auth_token"),
                )
                await send_response(writer, {"type": "regulization_request", "status_code": 200 if db_resp["status"] else 500, "data": db_resp})

            elif msg_type == "ongoing_session_data":
                logger.info(f"Fetching ongoing session data for session_id={header.get('session_id')}")
                db_resp = await get_ongoing_session_data(header.get("session_id"), header.get("auth_token"))
                await send_response(writer, {"type": "ongoing_session_data", "status_code": 200 if db_resp["status"] else 500, "data": db_resp})

            elif msg_type == "session_ended":
                logger.info(f"Ending session for session_id={header.get('session_id')}")
                db_resp = await end_session(header.get("session_id"), header.get("auth_token"))
                await send_response(writer, {"type": "session_ended", "status_code": 200 if db_resp["status"] else 500, "data": db_resp})

            elif msg_type == "incoming_audio_chunks":
                logger.info(f"Receiving incoming audio chunks for session_id={header.get('session_id')}")
                audio_length = header.get("audio_length")
                if audio_length is None:
                    logger.error("audio_length missing in incoming_audio_chunks event.")
                    await send_response(writer, {"type": "incoming_audio_chunks", "status_code": 500, "message": "audio_length missing"})
                    continue
                audio_data = await reader.readexactly(audio_length)
                await process_incoming_audio(
                    header.get("session_id"),
                    header.get("auth_token"),
                    header.get("start_time"),
                    audio_data,
                    writer,
                )

            elif msg_type == "socket_connection":
                logger.info(f"Socket connection event from client {addr}")
                # Simple ACK back so the client knows we've accepted the connection.                
                await send_response(writer, {"type": "socket_connection", "msg": "Connection acknowledged", "status_code": 200})

            else:
                logger.warning(f"Unknown message type received: {msg_type}")
                await send_response(writer, {"type": "error", "message": f"Unknown message type {msg_type}"})
    except (asyncio.IncompleteReadError, ConnectionResetError):
        logger.info(f"Client {addr} disconnected")
    finally:
        writer.close()
        await writer.wait_closed()

# ---------------------------------------------------------------------------
# Server entry-point
# ---------------------------------------------------------------------------

async def main():
    logger.info(f"Starting audio server, listening on {SOCKET_PATH}")
    server = await asyncio.start_unix_server(handle_client, path=SOCKET_PATH)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    logger.info("Audio server entry point started.")
    asyncio.run(main())
