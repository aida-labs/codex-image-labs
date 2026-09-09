#!/usr/bin/env python3
"""Generate or edit an image through the configured third-party Codex provider.

Thin CLI over the sibling modules: ``client`` (auth and provider transport),
``tasks`` (async task parsing and polling), and ``validators`` (image and
artifact verification).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import client as client_module  # noqa: F401 - re-exported for tests and tooling
import tasks as tasks_module  # noqa: F401 - re-exported for tests and tooling
import validators as validators_module  # noqa: F401 - re-exported for tests and tooling
from client import (
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_MODEL,
    EDIT_ENDPOINT,
    EXPECTED_BASE_URL,
    GENERATE_ENDPOINT,
    IMAGE_MODELS,
    MODEL,
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    STANDARD_USER_AGENT,
    TASK_ENDPOINT,
    GenerationError,
    download_url,
    provider_token,
    redact,
    submit_task,
    task_url,
)
from tasks import task_id_from_submission, wait_for_task
from validators import (
    default_output,
    default_receipt_path,
    detect_and_validate_image,
    ensure_output_extension,
    sha256_file,
    temporary_output_path,
    write_json_atomically,
)

# Backward-compatibility re-exports: before the split every name below lived
# on this module, and tests/tooling still reference them via generate.py.
__all__ = [
    "client_module",
    "tasks_module",
    "validators_module",
    "CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_MODEL",
    "EDIT_ENDPOINT",
    "EXPECTED_BASE_URL",
    "GENERATE_ENDPOINT",
    "IMAGE_MODELS",
    "MODEL",
    "POLL_INTERVAL_SECONDS",
    "POLL_TIMEOUT_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "STANDARD_USER_AGENT",
    "TASK_ENDPOINT",
    "GenerationError",
    "download_url",
    "provider_token",
    "redact",
    "submit_task",
    "task_url",
    "task_id_from_submission",
    "wait_for_task",
    "default_output",
    "default_receipt_path",
    "detect_and_validate_image",
    "ensure_output_extension",
    "sha256_file",
    "temporary_output_path",
    "write_json_atomically",
    "positive_float",
    "parse_args",
    "print_success",
    "main",
]


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
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
