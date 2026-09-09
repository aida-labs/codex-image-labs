"""Image validation and local artifact helpers for image-labs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import GenerationError


IMAGE_EXTENSIONS = {
    "png": {".png"},
    "jpeg": {".jpeg", ".jpg"},
    "webp": {".webp"},
}

def validate_png(data: bytes) -> None:
    if len(data) < 45:
        raise GenerationError("Downloaded PNG is incomplete")
    position = 8
    first_chunk = True
    saw_iend = False
    while position < len(data):
        if len(data) - position < 12:
            raise GenerationError("Downloaded PNG has a truncated chunk")
        length = int.from_bytes(data[position : position + 4], "big")
        chunk_type = data[position + 4 : position + 8]
        chunk_end = position + 12 + length
        if chunk_end > len(data):
            raise GenerationError("Downloaded PNG has a truncated chunk")
        chunk_data = data[position + 8 : position + 8 + length]
        expected_crc = int.from_bytes(data[position + 8 + length : chunk_end], "big")
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise GenerationError("Downloaded PNG has an invalid chunk checksum")
        if first_chunk and chunk_type != b"IHDR":
            raise GenerationError("Downloaded PNG is missing IHDR")
        if chunk_type == b"IHDR":
            if (
                length != 13
                or int.from_bytes(chunk_data[:4], "big") == 0
                or int.from_bytes(chunk_data[4:8], "big") == 0
            ):
                raise GenerationError("Downloaded PNG has an invalid IHDR")
        if chunk_type == b"IEND":
            if length != 0 or chunk_end != len(data):
                raise GenerationError("Downloaded PNG has an invalid IEND")
            saw_iend = True
            break
        position = chunk_end
        first_chunk = False
    if not saw_iend:
        raise GenerationError("Downloaded PNG is incomplete")

def validate_jpeg(data: bytes) -> None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise GenerationError("Downloaded JPEG is incomplete")

    position = 2
    saw_eoi = False
    while position < len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker == 0x00:
            continue
        if marker == 0xD9:
            saw_eoi = position == len(data)
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(data):
            raise GenerationError("Downloaded JPEG has a truncated marker")
        segment_length = int.from_bytes(data[position : position + 2], "big")
        if segment_length < 2 or position + segment_length > len(data):
            raise GenerationError("Downloaded JPEG has a truncated marker")
        position += segment_length
    if not saw_eoi:
        raise GenerationError("Downloaded JPEG is incomplete")

def validate_webp(data: bytes) -> None:
    if len(data) < 12 or not data.startswith(b"RIFF") or data[8:12] != b"WEBP":
        raise GenerationError("Downloaded WebP is incomplete")
    declared_size = int.from_bytes(data[4:8], "little")
    if declared_size + 8 != len(data):
        raise GenerationError("Downloaded WebP has an invalid RIFF size")

def detect_and_validate_image(path: Path) -> tuple[str, str]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        image_format = "png"
        validate_png(data)
    elif data.startswith(b"\xff\xd8\xff"):
        image_format = "jpeg"
        validate_jpeg(data)
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        image_format = "webp"
        validate_webp(data)
    else:
        raise GenerationError("Downloaded payload is not a PNG, JPEG, or WebP image")

    sips = Path("/usr/bin/sips")
    if not sips.is_file():
        return image_format, "structural"
    try:
        result = subprocess.run(
            [str(sips), "--getProperty", "format", str(path)],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise GenerationError("System image decoder timed out") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown decoder error"
        raise GenerationError(f"System image decoder rejected the output: {detail}")
    match = re.search(r"(?im)^\s*format:\s*(\S+)", result.stdout)
    decoded_format = match.group(1).lower() if match else ""
    if decoded_format and decoded_format != image_format:
        raise GenerationError(
            f"System image decoder reported {decoded_format}, expected {image_format}"
        )
    return image_format, "sips"

def ensure_output_extension(output: Path, image_format: str) -> None:
    suffix = output.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS[image_format]:
        requested = suffix or "no extension"
        raise GenerationError(
            f"Output extension {requested} does not match returned {image_format}; choose a matching output name"
        )

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def temporary_output_path(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem or 'image'}-",
        suffix=".part",
        dir=output.parent,
    )
    os.close(descriptor)
    return Path(name)

def write_json_atomically(path: Path, value: dict) -> None:
    temporary = temporary_output_path(path)
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def default_output() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    nonce = uuid.uuid4().hex[:8]
    return Path.cwd() / "outputs" / "imagegen" / f"image-{stamp}-{nonce}.png"

def default_receipt_path(output: Path) -> Path:
    return output.with_name(f"{output.name}.receipt.json")
