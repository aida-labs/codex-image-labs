"""Provider transport for image-labs: auth, curl subprocess, submit/poll, download."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import uuid
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
        f"url = {json.dumps(url, ensure_ascii=False)}",
        f"request = {json.dumps(method, ensure_ascii=False)}",
        f"header = {json.dumps('Authorization: Bearer ' + token, ensure_ascii=False)}",
        f"output = {json.dumps(str(response_path), ensure_ascii=False)}",
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
                f"form = {json.dumps('model=' + model_value, ensure_ascii=False)}",
                f"form = {json.dumps('prompt=' + str(payload['prompt']), ensure_ascii=False)}",
                *(f"form = {json.dumps('image=@' + str(image), ensure_ascii=False)}" for image in images),
            ]
        )
        for field in ("size", "quality"):
            if payload.get(field):
                lines.append(f"form = {json.dumps(field + '=' + str(payload[field]), ensure_ascii=False)}")
        if mask:
            lines.append(f"form = {json.dumps('mask=@' + str(mask), ensure_ascii=False)}")
    elif payload_path:
        lines.extend(
            [
                f"header = {json.dumps('Content-Type: application/json', ensure_ascii=False)}",
                f"data-binary = {json.dumps('@' + str(payload_path), ensure_ascii=False)}",
            ]
        )
    if user_agent:
        lines.append(f"user-agent = {json.dumps(user_agent, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"

CURL_INSTALL_HINT = (
    "Curl is not available on PATH. Install curl to use image-labs "
    "(Windows 10 1803+ includes curl.exe, otherwise `winget install cURL.cURL`; "
    "macOS includes curl; Linux: use your package manager, e.g. `sudo apt install curl`)."
)


def ensure_curl_available() -> None:
    if shutil.which("curl") is None:
        raise GenerationError(CURL_INSTALL_HINT)


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
    ensure_curl_available()
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
            encoding="utf-8",
            errors="replace",
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

def download_url(url: str, destination: Path) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "data":
        try:
            header, encoded = url.split(",", 1)
            if ";base64" in header.lower():
                destination.write_bytes(base64.b64decode(unquote_to_bytes(encoded), validate=True))
            else:
                destination.write_bytes(unquote_to_bytes(encoded))
        except (ValueError, binascii.Error) as exc:
            raise GenerationError("Provider returned invalid data URL image data") from exc
        return
    if parsed.scheme not in {"http", "https"}:
        raise GenerationError(f"Provider returned unsupported image URL scheme: {parsed.scheme}")

    ensure_curl_available()
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
            encoding="utf-8",
            errors="replace",
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
