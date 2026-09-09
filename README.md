# Image Labs

`image-labs` is a Codex skill for generating or editing raster images through the configured asynchronous third-party `gpt-image-2` provider.

## What It Does

- Submits image-generation requests to `/v1/images/generations/async`.
- Submits image-edit requests to `/v1/images/edits/async`.
- Polls `/v1/images/tasks/{task_id}` until a valid image result is available.
- Downloads, validates, hashes, and records a local image receipt without storing prompts, Bearer tokens, task IDs, or signed result URLs.

## Install

Clone the repository into the Codex skills directory:

```bash
git clone https://github.com/aida-labs/codex-image-labs.git "${CODEX_HOME:-$HOME/.codex}/skills/image-labs"
```

For later updates, use a fast-forward-only pull and run the offline regression suite:

```bash
git -C "${CODEX_HOME:-$HOME/.codex}/skills/image-labs" pull --ff-only
python3 -m unittest discover -s "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/tests" -v
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

The repository currently has no explicit software license. Public visibility does not grant reuse rights beyond those allowed by applicable law.
