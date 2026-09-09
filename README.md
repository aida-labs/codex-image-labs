# Image Labs

`image-labs` is a Codex skill for generating or editing raster images through the configured asynchronous third-party provider. It defaults to `gpt-image-2.5-sunburst`; `gpt-image-2.5-flare` is the faster everyday alternative and `gpt-image-2` remains for legacy compatibility.

Current skill version: `0.1.0` (see [`VERSION`](VERSION)).

## What It Does

- Submits image-generation requests to `/v1/images/generations/async`.
- Submits image-edit requests to `/v1/images/edits/async`.
- Polls `/v1/images/tasks/{task_id}` until a valid image result is available.
- Downloads, validates, hashes, and records a local image receipt without storing prompts, Bearer tokens, task IDs, or signed result URLs.

## Prerequisites

- Python 3.11 or newer (`python3 --version`; on Windows use `python --version` or `py -3 --version`). The helper reads `config.toml` with the standard-library `tomllib` module and keeps zero third-party dependencies.
- `curl` on `PATH`. It is preinstalled on macOS and on Windows 10 1803+; on a Windows machine without curl run `winget install cURL.cURL`; on Linux use your package manager, for example `sudo apt install curl`.
- `git` for installation and updates (`git --version`).

## Install

Clone the repository into the Codex skills directory:

```bash
git clone https://github.com/aida-labs/codex-image-labs.git "${CODEX_HOME:-$HOME/.codex}/skills/image-labs"
```

Windows PowerShell equivalent:

```powershell
$skillsRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME "skills" } else { Join-Path $HOME ".codex\skills" }
git clone https://github.com/aida-labs/codex-image-labs.git (Join-Path $skillsRoot "image-labs")
```

For later updates, use the bundled safe updater. It prepares a temporary candidate, runs the offline regression suite, replaces the target only after the tests pass, and keeps the previous directory as a timestamped backup:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/scripts/update.py" \
  --target "${CODEX_HOME:-$HOME/.codex}/skills/image-labs"
```

Windows PowerShell equivalent (use `py -3` if `python` is not on `PATH`):

```powershell
$skillsRoot = if ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME "skills" } else { Join-Path $HOME ".codex\skills" }
$skillPath = Join-Path $skillsRoot "image-labs"
python (Join-Path $skillPath "scripts\update.py") --target $skillPath
```

Check an update without changing the installed directory:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/scripts/update.py" \
  --target "${CODEX_HOME:-$HOME/.codex}/skills/image-labs" \
  --check
```

Show the installed version and revision:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/scripts/update.py" \
  --target "${CODEX_HOME:-$HOME/.codex}/skills/image-labs" \
  --version
```

If the existing directory was copied by another installer and has no `.git`, bootstrap the updater once from a temporary clone. The updater will migrate the directory to a Git-backed installation and preserve the old directory as a backup:

```bash
bootstrap="$(mktemp -d)"
git clone --depth 1 https://github.com/aida-labs/codex-image-labs.git "$bootstrap"
python3 "$bootstrap/scripts/update.py" \
  --target "${CODEX_HOME:-$HOME/.codex}/skills/image-labs"
```

After a successful update, start a new Codex task so the new skill instructions are loaded. The updater prints the backup path. To restore it:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/scripts/update.py" \
  --target "${CODEX_HOME:-$HOME/.codex}/skills/image-labs" \
  --rollback "/path/printed/by/the/updater"
```

The PowerShell equivalents use the same `--check`, `--version`, and `--rollback` options; set `$skillsRoot` as shown above and use `python` or `py -3`.

## Configuration

The helper expects the active Codex provider configuration to use `https://api.lsidestudio.com/v1` and to expose its Bearer credential through the current Codex provider configuration. It refuses a different base URL and never writes a credential to files, receipts, or output.

Do not commit `config.toml`, generated images, receipts, or provider credentials to this repository.

## Verification

Run the bundled offline suite before publishing changes or diagnosing an update:

```bash
python3 -m unittest discover -s "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/tests" -v
```

Windows PowerShell equivalent:

```powershell
python -m unittest discover -s "$env:USERPROFILE\.codex\skills\image-labs\tests" -v
```

The repository currently has no explicit software license. Public visibility does not grant reuse rights beyond those allowed by applicable law.
