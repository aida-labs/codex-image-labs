# Image Labs

`image-labs` is a Codex skill for generating or editing raster images through the configured asynchronous third-party provider. It defaults to `gpt-image-2.5-sunburst`; `gpt-image-2.5-flare` is the faster everyday alternative and `gpt-image-2` remains for legacy compatibility.

## What It Does

- Submits image-generation requests to `/v1/images/generations/async`.
- Submits image-edit requests to `/v1/images/edits/async`.
- Polls `/v1/images/tasks/{task_id}` until a valid image result is available.
- Downloads, validates, hashes, and records a local image receipt without storing prompts, Bearer tokens, task IDs, or signed result URLs.

## Prerequisites

- Python 3.11 or newer (`python3 --version`; on Windows use `python --version` or `py -3 --version`). The helper reads `config.toml` with the standard-library `tomllib` module and keeps zero third-party dependencies.
- `curl` on `PATH`. It is preinstalled on macOS and on Windows 10 1803+; on a Windows machine without curl run `winget install cURL.cURL`; on Linux use your package manager, for example `sudo apt install curl`.

## Install

Clone the repository into the Codex skills directory:

```bash
git clone https://github.com/aida-labs/codex-image-labs.git "${CODEX_HOME:-$HOME/.codex}/skills/image-labs"
```

Windows PowerShell equivalent:

```powershell
git clone https://github.com/aida-labs/codex-image-labs.git "$env:USERPROFILE\.codex\skills\image-labs"
```

For later updates, use a fast-forward-only pull and run the offline regression suite:

```bash
git -C "${CODEX_HOME:-$HOME/.codex}/skills/image-labs" pull --ff-only
python3 -m unittest discover -s "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/tests" -v
```

Windows PowerShell equivalent (use `py -3` if `python` is not on `PATH`):

```powershell
git -C "$env:USERPROFILE\.codex\skills\image-labs" pull --ff-only
python -m unittest discover -s "$env:USERPROFILE\.codex\skills\image-labs\tests" -v
```

Start a new Codex task after an update so the newly installed skill instructions are used.

## Configuration

The helper expects the active Codex provider configuration to use `https://api.lsidestudio.com/v1` and to expose its Bearer credential through the current Codex provider configuration. It refuses a different base URL and never writes a credential to files, receipts, or output.

Do not commit `config.toml`, generated images, receipts, or provider credentials to this repository.

## Verification

Run the bundled offline suite before publishing changes:

```bash
python3 -m unittest discover -s "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/tests" -v
```

Windows PowerShell equivalent:

```powershell
python -m unittest discover -s "$env:USERPROFILE\.codex\skills\image-labs\tests" -v
```

The repository currently has no explicit software license. Public visibility does not grant reuse rights beyond those allowed by applicable law.
