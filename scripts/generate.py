#!/usr/bin/env python3
"""Generate or edit an image through the configured third-party Codex provider."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

BASE_URL = "https://api.lsidestudio.com/v1"
GENERATE_ENDPOINT = f"{BASE_URL}/images/generations/async"
EDIT_ENDPOINT = f"{BASE_URL}/images/edits/async"
TASK_ENDPOINT = f"{BASE_URL}/images/tasks"
EXPECTED_BASE_URL = BASE_URL
DEFAULT_MODEL = "gpt-image-2.5-sunburst"
MODEL = DEFAULT_MODEL
IMAGE_MODELS = ("gpt-image-2.5-sunburst", "gpt-image-2.5-flare", "gpt-image-2")
STANDARD_USER_AGENT = "curl/8.7.1"
CONNECT_TIMEOUT_SECONDS = 15
REQUEST_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 2.0
POLL_TIMEOUT_SECONDS = 600.0
IMAGE_EXTENSIONS = {
    "png": {".png"},
    "jpeg": {".jpeg", ".jpg"},
    "webp": {".webp"},
}


class GenerationError(RuntimeError):
    pass


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(
        r"(?i)(authorization:\s*bearer\s+)[^\s\"']+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)([?&](?:access_token|signature|sig|token|x-amz-signature)=)[^&\s\"']+",
        r"\1[REDACTED]",
        text,
    )
    return text


def codex_config_path() -> Path:
    explicit = os.environ.get("CODEX_CONFIG")
    if explicit:
        return Path(explicit).expanduser()

    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def provider_token() -> str:
    config_path = codex_config_path()
    if not config_path.is_file():
        raise GenerationError(f"Codex config not found: {config_path}")

    if tomllib is None:
        raise GenerationError("Python 3.11 or newer is required to read Codex config")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise GenerationError(f"Cannot read Codex config: {exc}") from exc

    provider_name = config.get("model_provider")
    provider = config.get("model_providers", {}).get(provider_name, {})
    base_url = str(provider.get("base_url", "")).rstrip("/")
    if base_url != EXPECTED_BASE_URL:
        observed = base_url or "missing"
        raise GenerationError(
            f"Current provider base URL is {observed}, not the required third-party endpoint"
        )

    token = provider.get("experimental_bearer_token")
    if not token and provider.get("env_key"):
        token = os.environ.get(str(provider["env_key"]))
    if not isinstance(token, str) or not token.strip():
        raise GenerationError("No Bearer key is available for the current Codex provider")
    return token.strip()


def curl_config(
    token: str,
    response_path: Path,
    user_agent: str | None,
    *,
    url: str,
    method: str,
    payload: dict | None = None,
    payload_path: Path | None = None,
    images: tuple[Path, ...] = (),
    mask: Path | None = None,
) -> str:
    lines = [
        f"url = {json.dumps(url)}",
        f"request = {json.dumps(method)}",
        f"header = {json.dumps('Authorization: Bearer ' + token)}",
        f"output = {json.dumps(str(response_path))}",
        'write-out = "%{http_code}"',
        f"connect-timeout = {CONNECT_TIMEOUT_SECONDS}",
        f"max-time = {REQUEST_TIMEOUT_SECONDS}",
        "silent",
        "show-error",
    ]
    if images:
        if method != "POST" or payload is None:
            raise GenerationError("Image uploads require a POST request payload")
        model_value = str((payload or {}).get("model", DEFAULT_MODEL))
        lines.extend(
            [
                f"form = {json.dumps('model=' + model_value)}",
                f"form = {json.dumps('prompt=' + str(payload['prompt']))}",
                *(f"form = {json.dumps('image=@' + str(image))}" for image in images),
            ]
        )
        for field in ("size", "quality"):
            if payload.get(field):
                lines.append(f"form = {json.dumps(field + '=' + str(payload[field]))}")
        if mask:
            lines.append(f"form = {json.dumps('mask=@' + str(mask))}")
    elif payload_path:
        lines.extend(
            [
                f"header = {json.dumps('Content-Type: application/json')}",
                f"data-binary = {json.dumps('@' + str(payload_path))}",
            ]
        )
    if user_agent:
        lines.append(f"user-agent = {json.dumps(user_agent)}")
    return "\n".join(lines) + "\n"


def request(
    *,
    url: str,
    method: str,
    token: str,
    temp_dir: Path,
    user_agent: str | None,
    payload: dict | None = None,
    images: tuple[Path, ...] = (),
    mask: Path | None = None,
) -> tuple[int, str]:
    payload_path = None
    if payload is not None and not images:
        payload_path = temp_dir / f"request-{uuid.uuid4().hex}.json"
        payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    response_path = temp_dir / f"response-{uuid.uuid4().hex}.json"
    try:
        result = subprocess.run(
            ["curl", "--config", "-"],
            input=curl_config(
                token,
                response_path,
                user_agent,
                url=url,
                method=method,
                payload=payload,
                payload_path=payload_path,
                images=images,
                mask=mask,
            ),
            text=True,
            capture_output=True,
            check=False,
            timeout=REQUEST_TIMEOUT_SECONDS + CONNECT_TIMEOUT_SECONDS + 15,
        )
    except subprocess.TimeoutExpired as exc:
        raise GenerationError("Provider request timed out") from exc
    except OSError as exc:
        raise GenerationError(f"Cannot start curl for provider request: {exc}") from exc

    status_text = result.stdout.strip()
    try:
        status = int(status_text)
    except ValueError:
        detail = result.stderr.strip() or "curl did not return an HTTP status"
        raise GenerationError(detail) from None

    body = response_path.read_text(encoding="utf-8", errors="replace") if response_path.exists() else ""
    if result.returncode != 0 and not body:
        raise GenerationError(result.stderr.strip() or f"curl failed with exit code {result.returncode}")
    return status, body


def is_cloudflare_1010(status: int, body: str) -> bool:
    if status != 403:
        return False
    lowered = body.lower()
    return "1010" in lowered or "browser integrity" in lowered


def request_with_cloudflare_retry(
    *,
    url: str,
    method: str,
    token: str,
    temp_dir: Path,
    user_agent: str | None,
    payload: dict | None = None,
    images: tuple[Path, ...] = (),
    mask: Path | None = None,
) -> tuple[int, str, str | None]:
    status, body = request(
        url=url,
        method=method,
        token=token,
        temp_dir=temp_dir,
        user_agent=user_agent,
        payload=payload,
        images=images,
        mask=mask,
    )
    if is_cloudflare_1010(status, body) and user_agent is None:
        status, body = request(
            url=url,
            method=method,
            token=token,
            temp_dir=temp_dir,
            user_agent=STANDARD_USER_AGENT,
            payload=payload,
            images=images,
            mask=mask,
        )
        return status, body, STANDARD_USER_AGENT
    return status, body, user_agent


def submit_task(
    payload: dict,
    token: str,
    temp_dir: Path,
    user_agent: str | None,
    images: tuple[Path, ...] = (),
    mask: Path | None = None,
) -> tuple[int, str, str | None]:
    return request_with_cloudflare_retry(
        url=EDIT_ENDPOINT if images else GENERATE_ENDPOINT,
        method="POST",
        token=token,
        temp_dir=temp_dir,
        user_agent=user_agent,
        payload=payload,
        images=images,
        mask=mask,
    )


def task_url(task_id: str) -> str:
    return f"{TASK_ENDPOINT}/{quote(task_id, safe='')}"


def poll_task(
    task_id: str,
    token: str,
    temp_dir: Path,
    user_agent: str | None,
) -> tuple[int, str, str | None]:
    return request_with_cloudflare_retry(
        url=task_url(task_id),
        method="GET",
        token=token,
        temp_dir=temp_dir,
        user_agent=user_agent,
    )


def parse_json_object(response_body: str, *, context: str) -> dict:
    try:
        response = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"Provider {context} response is not JSON") from exc
    if not isinstance(response, dict):
        raise GenerationError(f"Provider {context} response is not a JSON object")
    return response


def task_id_from_submission(response_body: str) -> str:
    response = parse_json_object(response_body, context="submission")
    candidates = [response, response.get("task"), response.get("data")]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        task_id = candidate.get("task_id", candidate.get("taskId"))
        if isinstance(task_id, (str, int)) and str(task_id).strip():
            return str(task_id).strip()
    raise GenerationError("Provider submission response has no task_id")


def task_status(response: dict) -> str | None:
    candidates = [response, response.get("task"), response.get("data")]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        value = candidate.get("status", candidate.get("state"))
        if isinstance(value, str) and value.strip():
            return value.strip().lower().replace("-", "_").replace(" ", "_")
    return None


def image_from_item(item: dict) -> tuple[str, bytes | str] | None:
    if isinstance(item.get("url"), str) and item["url"]:
        source = "data_url" if urlparse(item["url"]).scheme == "data" else "remote_url"
        return source, item["url"]
    if isinstance(item.get("b64_json"), str) and item["b64_json"]:
        try:
            return "inline_base64", base64.b64decode(item["b64_json"], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise GenerationError("Provider returned invalid base64 image data") from exc
    return None


def image_from_response(value: object) -> tuple[str, bytes | str] | None:
    if isinstance(value, list):
        for item in value:
            image = image_from_response(item)
            if image is not None:
                return image
        return None
    if not isinstance(value, dict):
        return None

    image = image_from_item(value)
    if image is not None:
        return image
    for key in ("data", "result", "output", "response", "image"):
        image = image_from_response(value.get(key))
        if image is not None:
            return image
    return None


def parse_image(response_body: str) -> tuple[str, bytes | str]:
    image = image_from_response(parse_json_object(response_body, context="task"))
    if image is None:
        raise GenerationError("Provider task result has no image data")
    return image


PENDING_TASK_STATES = {
    "created",
    "pending",
    "queued",
    "running",
    "processing",
    "in_progress",
    "submitted",
}
SUCCESS_TASK_STATES = {"completed", "done", "finished", "success", "succeeded"}
FAILED_TASK_STATES = {"cancelled", "canceled", "error", "expired", "failed"}


def wait_for_task(
    task_id: str,
    token: str,
    temp_dir: Path,
    user_agent: str | None,
    poll_interval_seconds: float,
    poll_timeout_seconds: float,
) -> tuple[int, tuple[str, bytes | str], int, str | None, str | None, float]:
    started_at = time.monotonic()
    deadline = started_at + poll_timeout_seconds
    poll_count = 0
    current_user_agent = user_agent
    while True:
        status, body, current_user_agent = poll_task(
            task_id, token, temp_dir, current_user_agent
        )
        if not 200 <= status < 300:
            raise GenerationError(f"Provider task poll returned HTTP {status}")
        poll_count += 1
        response = parse_json_object(body, context="task")
        state = task_status(response)
        image = image_from_response(response)
        if image is not None:
            if state in PENDING_TASK_STATES:
                raise GenerationError(
                    f"Provider task returned image data with nonterminal status {state}"
                )
            if state in FAILED_TASK_STATES:
                raise GenerationError(f"Provider task ended with status {state}")
            return (
                status,
                image,
                poll_count,
                current_user_agent,
                state,
                max(0.0, time.monotonic() - started_at),
            )

        if state in SUCCESS_TASK_STATES:
            raise GenerationError("Provider task completed without image data")
        if state in FAILED_TASK_STATES:
            raise GenerationError(f"Provider task ended with status {state}")
        if state not in PENDING_TASK_STATES:
            raise GenerationError("Provider task response has no supported status or image data")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GenerationError(
                f"Provider task did not finish within {poll_timeout_seconds:g} seconds"
            )
        time.sleep(min(poll_interval_seconds, remaining))


def download_url(url: str, destination: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "data":
        try:
            header, encoded = url.split(",", 1)
            if ";base64" in header.lower():
                destination.write_bytes(base64.b64decode(encoded, validate=True))
            else:
                destination.write_bytes(unquote_to_bytes(encoded))
        except (ValueError, binascii.Error) as exc:
            raise GenerationError("Provider returned invalid data URL image data") from exc
        return
    if parsed.scheme not in {"http", "https"}:
        raise GenerationError(f"Provider returned unsupported image URL scheme: {parsed.scheme}")

    try:
        result = subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--silent",
                "--show-error",
                "--connect-timeout",
                str(CONNECT_TIMEOUT_SECONDS),
                "--max-time",
                str(REQUEST_TIMEOUT_SECONDS),
                "--user-agent",
                STANDARD_USER_AGENT,
                "--output",
                str(destination),
                url,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=REQUEST_TIMEOUT_SECONDS + CONNECT_TIMEOUT_SECONDS + 15,
        )
    except subprocess.TimeoutExpired as exc:
        destination.unlink(missing_ok=True)
        raise GenerationError("Image download timed out") from exc
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise GenerationError(f"Cannot start curl for image download: {exc}") from exc
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        raise GenerationError(result.stderr.strip() or "Image download failed")


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


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or edit an image through the current third-party Codex provider."
    )
    parser.add_argument("--prompt", required=True, help="Image generation prompt")
    parser.add_argument("--image", action="append", type=Path, help="Input image for editing; repeat for multiple images")
    parser.add_argument("--mask", type=Path, help="Optional mask image for localized editing")
    parser.add_argument("--output", type=Path, default=default_output(), help="Local output image file")
    parser.add_argument("--receipt", type=Path, help="Local JSON receipt path; defaults beside --output")
    parser.add_argument("--no-receipt", action="store_true", help="Do not create a JSON receipt")
    parser.add_argument("--json", action="store_true", help="Emit only the final artifact receipt as JSON")
    parser.add_argument("--size", help="Optional provider-supported size, for example 1024x1024")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Image model; defaults to gpt-image-2.5-sunburst. Use gpt-image-2.5-flare for faster everyday generation or gpt-image-2 for legacy compatibility.",
    )
    parser.add_argument("--quality", choices=("low", "medium", "high", "xhigh", "max", "auto"), help="Optional image quality")
    parser.add_argument(
        "--poll-interval-seconds",
        type=positive_float,
        default=POLL_INTERVAL_SECONDS,
        help="Seconds between task polls; defaults to 2",
    )
    parser.add_argument(
        "--poll-timeout-seconds",
        type=positive_float,
        default=POLL_TIMEOUT_SECONDS,
        help="Maximum task polling duration; defaults to 600",
    )
    parser.add_argument("--force", action="store_true", help="Replace an existing output image and receipt")
    return parser.parse_args()


def print_success(receipt: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return
    print(f"saved: {receipt['output']}")
    if receipt["receipt"]:
        print(f"receipt: {receipt['receipt']}")
    print(f"model: {receipt['model']}")
    print(f"mode: {receipt['mode']}")
    print(f"submit_http_status: {receipt['submit_http_status']}")
    print(f"task_http_status: {receipt['task_http_status']}")
    print(f"task_status: {receipt['task_status'] or 'not_returned'}")
    print(f"task_polls: {receipt['task_polls']}")
    print(f"task_wait_seconds: {receipt['task_wait_seconds']}")
    print(f"http_status: {receipt['http_status']}")
    print(f"source: {receipt['source']}")
    print(f"downloaded: {'yes' if receipt['downloaded'] else 'no'}")
    print(f"format: {receipt['format']}")
    print(f"bytes: {receipt['bytes']}")
    print(f"sha256: {receipt['sha256']}")
    print(f"verified_by: {receipt['verified_by']}")


def main() -> int:
    args = parse_args()
    token = ""
    temporary_image: Path | None = None
    try:
        if args.receipt and args.no_receipt:
            raise GenerationError("--receipt and --no-receipt cannot be used together")

        output = args.output.expanduser().resolve()
        receipt_path = None
        if not args.no_receipt:
            receipt_path = (args.receipt or default_receipt_path(output)).expanduser().resolve()
            if receipt_path == output:
                raise GenerationError("Receipt path cannot equal output image path")

        if output.exists() and not args.force:
            raise GenerationError(
                f"Output exists; choose another path or use --force explicitly: {output}"
            )
        if receipt_path and receipt_path.exists() and not args.force:
            raise GenerationError(
                f"Receipt exists; choose another path or use --force explicitly: {receipt_path}"
            )

        token = provider_token()
        images = tuple(path.expanduser().resolve() for path in (args.image or []))
        mask = args.mask.expanduser().resolve() if args.mask else None
        missing = [str(path) for path in (*images, *([mask] if mask else [])) if not path.is_file()]
        if missing:
            raise GenerationError(f"Input image not found: {', '.join(missing)}")
        if mask and not images:
            raise GenerationError("--mask requires at least one --image")

        payload = {"model": args.model or DEFAULT_MODEL, "prompt": args.prompt}
        if args.size:
            payload["size"] = args.size
        if args.quality:
            payload["quality"] = args.quality

        output.parent.mkdir(parents=True, exist_ok=True)
        if receipt_path:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="image-labs-") as temp_name:
            temp_dir = Path(temp_name)
            submit_status, body, user_agent = submit_task(
                payload, token, temp_dir, None, images=images, mask=mask
            )
            if not 200 <= submit_status < 300:
                raise GenerationError(f"Provider submission returned HTTP {submit_status}")
            task_id = task_id_from_submission(body)
            (
                task_status_code,
                image_result,
                task_polls,
                _,
                task_state,
                task_wait_seconds,
            ) = wait_for_task(
                task_id,
                token,
                temp_dir,
                user_agent,
                args.poll_interval_seconds,
                args.poll_timeout_seconds,
            )
            source, image = image_result
            temporary_image = temporary_output_path(output)
            if source == "inline_base64":
                temporary_image.write_bytes(image)
            else:
                download_url(image, temporary_image)
            if temporary_image.stat().st_size == 0:
                raise GenerationError("Provider returned an empty image")
            image_format, verified_by = detect_and_validate_image(temporary_image)
            ensure_output_extension(output, image_format)
            byte_count = temporary_image.stat().st_size
            sha256 = sha256_file(temporary_image)
            temporary_image.replace(output)
            temporary_image = None

        receipt = {
            "bytes": byte_count,
            "downloaded": source == "remote_url",
            "format": image_format,
            "http_status": task_status_code,
            "mode": "edit" if images else "generate",
            "model": args.model or DEFAULT_MODEL,
            "output": str(output),
            "receipt": str(receipt_path) if receipt_path else None,
            "sha256": sha256,
            "source": source,
            "submit_http_status": submit_status,
            "task_http_status": task_status_code,
            "task_polls": task_polls,
            "task_status": task_state,
            "task_wait_seconds": round(task_wait_seconds, 3),
            "verified_by": verified_by,
        }
        if receipt_path:
            try:
                write_json_atomically(receipt_path, receipt)
            except OSError as exc:
                raise GenerationError(
                    f"Image was saved at {output}, but its receipt could not be written: {exc}"
                ) from exc
        print_success(receipt, args.json)
        return 0
    except (GenerationError, OSError) as exc:
        if temporary_image:
            temporary_image.unlink(missing_ok=True)
        print(f"error: {redact(str(exc), (token,))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
