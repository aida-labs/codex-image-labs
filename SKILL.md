---
name: image-labs
description: "Generate or edit raster images through the configured third-party model provider when the user asks for API-based image work or explicitly requests the third-party route. Use for photos, illustrations, product shots, mockups, covers, and other bitmap assets; do not use for vector/code-native graphics."
---

# Image Labs

Use this skill when a user wants a bitmap image generated or edited through the current third-party provider, especially when they say "走第三方", "用供应商", or invoke `$image-labs`. It is intentionally separate from the general `imagegen` skill, which may use Codex's built-in image tool.

For installation and updates, use the repository's `scripts/update.py` safe updater. It tests a temporary candidate before replacing the installed skill and keeps a rollback backup. See [README.md](README.md) for platform-specific commands.

## Provider Contract

- Use `gpt-image-2.5-sunburst` by default with the asynchronous provider endpoints: `POST https://api.lsidestudio.com/v1/images/generations/async` for generation and `POST https://api.lsidestudio.com/v1/images/edits/async` for editing. Their provider-relative aliases are `/images/generations/async` and `/images/edits/async`. `gpt-image-2.5-flare` is the faster everyday alternative and `gpt-image-2` remains only for legacy compatibility; pass `--model` explicitly when the user requests one of them. Do not silently substitute another model, endpoint, local generator, or built-in image tool.
- Read the Bearer key from the current user's Codex provider configuration at execution time. The helper rejects a missing or mismatched provider `base_url`; never copy a token into source files, prompts, logs, artifacts, or the response.
- Generation submits JSON with `model` and `prompt`. Editing submits multipart form data with `model`, `prompt`, one or more `image` files, and an optional `mask`; pass optional `size` and `quality` only when useful and supported.
- A successful submission only creates a task. Extract its nonempty `task_id`, then poll `GET https://api.lsidestudio.com/v1/images/tasks/{task_id}` (provider-relative alias: `/images/tasks/{task_id}`) until the task returns its image payload or an explicit terminal failure. The task ID is used only for the request path and is not persisted in the user-facing receipt. Do not treat `202`, a task ID, or a queued/running status as an image deliverable.
- Completion requires a validated local image artifact, a local receipt, and a user-visible final response that renders the saved image and links its source file.

## Delivery Contract

For every generated or edited image, all of the following are mandatory before saying it is complete:

1. Save the image to an explicit, descriptive path under the current task's `outputs/imagegen/` directory. Do not rely on the helper's fallback name when an agent is serving a user. Do not write outside the workspace unless the user explicitly requested that destination.
2. Run the helper with `--json`, then read its final receipt. Exit code `0` is necessary but not sufficient: the receipt must identify the absolute output path, image format, byte count, SHA-256, submission and terminal-task HTTP statuses, returned task status when available, poll count, task wait seconds, source type, and decoder verification.
3. Keep the default sibling receipt file, `<image>.receipt.json`. It deliberately excludes the prompt, Bearer token, and signed provider URL. Do not use `--no-receipt` for user-facing work.
4. In the final answer, embed the image using its absolute local path and provide a clickable source-file path. State whether it was downloaded from a remote URL or written from inline base64. Include format, byte count, SHA-256, submission and terminal-task HTTP statuses, and any limitation.
5. If the helper errors, if its receipt is missing, or if visual inspection shows a problem, report the artifact as incomplete. Do not claim success based only on an HTTP status, model availability, or a remote URL.

Use this final-response shape after a successful run:

```markdown
![Generated image](/absolute/path/to/outputs/imagegen/descriptive-name.png)

File: [/absolute/path/to/outputs/imagegen/descriptive-name.png](/absolute/path/to/outputs/imagegen/descriptive-name.png)
Receipt: [/absolute/path/to/outputs/imagegen/descriptive-name.png.receipt.json](/absolute/path/to/outputs/imagegen/descriptive-name.png.receipt.json)
Saved locally: yes
Source: downloaded from provider URL | written from inline base64
Format: PNG; size: 123456 bytes; SHA-256: <hash>; submit HTTP: 202; terminal task HTTP: 200; task state: completed; polls: 3; wait: 6.2 seconds
```

## Workflow

1. Infer the mode from inputs: no `--image` means generation; one or more `--image` values means editing. Use `--mask` when the user wants a localized edit; the mask must be a compatible image file.
2. Preserve the user's subject, style, composition, exact text, and constraints. For edits, state the invariants explicitly: change only the requested element and keep everything else unchanged. Do not invent brand names, slogans, people, or visual elements.
3. Choose a descriptive, non-conflicting output name such as `outputs/imagegen/quiet-library-study.png`. Do not overwrite an existing image or receipt unless the user explicitly requested replacement and `--force` is justified.
4. Run the bundled helper. It submits one asynchronous task, polls its task endpoint to a terminal image result, then saves URL-based output only after the download completes. It validates PNG/JPEG/WebP structure, uses the macOS system decoder when available, atomically moves the verified image to the output path, and writes a receipt. The receipt's `verified_by` field is `sips` on macOS and `structural` on systems without the macOS decoder.

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/scripts/generate.py" \
     --prompt "<user prompt>" \
     --output outputs/imagegen/<descriptive-name>.png \
     --json
   ```

   Windows PowerShell equivalent (use `py -3` if `python` is not on `PATH`):

   ```powershell
   python "$env:USERPROFILE\.codex\skills\image-labs\scripts\generate.py" `
     --prompt "<user prompt>" `
     --output outputs/imagegen/<descriptive-name>.png `
     --json
   ```

   Requires Python 3.11 or newer and `curl` on `PATH` (preinstalled on macOS and Windows 10 1803+; otherwise `winget install cURL.cURL` on Windows or the system package manager on Linux).

   The helper defaults to `--model gpt-image-2.5-sunburst`. Pass `--model gpt-image-2.5-flare` only when the user explicitly wants faster everyday generation, or `--model gpt-image-2` for legacy compatibility.

   Editing example:

   ```bash
   python3 "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/scripts/generate.py" \
     --prompt "Change only the background to a clean studio gray; preserve the product, edges, camera angle, and lighting." \
     --image /path/to/source.png \
     --mask /path/to/mask.png \
     --output outputs/imagegen/product-edited.png \
     --json
   ```

   Use `--size` or `--quality` only when useful (`--quality` supports `low`, `medium`, `high`, `xhigh`, `max`, and `auto`). The default polling cadence is two seconds and the default task timeout is ten minutes; use `--poll-interval-seconds` or `--poll-timeout-seconds` only when the requested run needs a different bounded wait. A live generation validation completed after 47 polls: its 46 configured waits alone account for at least 92 seconds, and the end-to-end run took roughly 1-2 minutes. Treat that as a current successful sample, not an upstream latency SLA; do not report failure merely because a task takes dozens of polls. Use `--force` only for an explicit replacement. The helper's default receipt path is `<output>.receipt.json`; specify `--receipt` only when the user requires a different local receipt path.
5. Read the receipt, inspect the saved image when visual quality matters, then send the user-facing result following the delivery contract. Do not expose a signed image URL, raw provider response, or credentials.

## Failure Handling

- The helper checks submission and polling HTTP statuses, `task_id`, task status, JSON shape, image payload, image completeness, output format, and receipt creation. Its receipt records `task_status` when returned and measured `task_wait_seconds`; these are observability fields, not a provider SLA. It reports file-system, task-timeout, and provider failures as redacted `error:` messages rather than Python tracebacks.
- If a provider request is rejected with Cloudflare error `1010` during submission or polling, retry exactly once with a standard curl API User-Agent and retain that User-Agent for later polls. The retry uses a fixed standard curl User-Agent string regardless of the locally installed curl version. If that retry fails, stop that image call and report the provider error with credentials omitted. Continue independent authorized work that does not depend on the missing image; clearly mark the affected deliverable as incomplete until its required asset and verification are available.
- If the task returns an unsupported response shape, never infer completion from elapsed time or HTTP `200`: stop with a contract error. The helper accepts the existing `data[].url` / `b64_json` image result shape and common `result`, `output`, or `response` wrappers; update it against a verified provider sample before broadening that parser. If a returned task state contradicts its image payload, such as `processing` or `failed` alongside an image, stop rather than reporting a false completion.
- Do not fall back silently. A provider failure is a provider failure; ask before using another route.

## Prompt Shape

For a vague request, organize the prompt as: subject and action, scene/background, visual medium, composition/framing, lighting/mood, intended use, exact text, and constraints/avoid list. Keep the final prompt concise. For exact in-image text, quote it verbatim and require accurate spelling.

The reusable execution path is a thin CLI in [scripts/generate.py](scripts/generate.py) over three modules: [scripts/client.py](scripts/client.py) (provider auth, curl transport, async submit/poll, download), [scripts/tasks.py](scripts/tasks.py) (task response parsing and polling to completion), and [scripts/validators.py](scripts/validators.py) (image structure plus system-decoder verification and local artifact files). Run its offline regression suite with:

```bash
python3 -m unittest discover -s "${CODEX_HOME:-$HOME/.codex}/skills/image-labs/tests" -v
```

On Windows PowerShell, replace `python3` with `python` (or `py -3`) and `${CODEX_HOME:-$HOME/.codex}` with `$env:CODEX_HOME` (or `$env:USERPROFILE\.codex`).
