"""Task protocol for image-labs: response parsing and polling to completion."""

from __future__ import annotations

import base64
import binascii
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from client import GenerationError, poll_task


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
