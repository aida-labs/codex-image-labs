from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).with_name("generate.py")
if not SCRIPT.is_file():
    SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate.py"


def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def build_valid_png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")
    return b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", ihdr) + png_chunk(b"IDAT", idat) + png_chunk(b"IEND", b"")


VALID_PNG = build_valid_png()


def load_fresh_client():
    path = Path(__file__).resolve().parents[1] / "scripts" / "client.py"
    spec = importlib.util.spec_from_file_location("image_labs_client_fresh", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class GenerateImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="image-labs-tests-")
        self.root = Path(self.temp.name)
        self.module = load_module(f"image_labs_test_{id(self)}")
        self.old_argv = sys.argv
        self.old_config = os.environ.get("CODEX_CONFIG")

    def tearDown(self) -> None:
        sys.argv = self.old_argv
        if self.old_config is None:
            os.environ.pop("CODEX_CONFIG", None)
        else:
            os.environ["CODEX_CONFIG"] = self.old_config
        self.temp.cleanup()

    def write_config(self, provider: str) -> None:
        config = self.root / "config.toml"
        config.write_text(provider, encoding="utf-8")
        os.environ["CODEX_CONFIG"] = str(config)

    def task_submission(self, task_id: str = "task-123") -> str:
        return json.dumps({"task_id": task_id, "status": "queued"})

    def inline_body(self, data: bytes = VALID_PNG, *, status: str | None = None) -> str:
        body = {"data": [{"b64_json": base64.b64encode(data).decode("ascii")} ]}
        if status:
            body["status"] = status
        return json.dumps(body)

    def run_main(
        self,
        target: Path,
        submit_body: str,
        task_bodies: list[str],
        *,
        submit_status: int = 202,
        task_statuses: list[int] | None = None,
        extra_args: list[str] | None = None,
    ):
        self.module.provider_token = lambda: "fixture-token"
        statuses = task_statuses or [200] * len(task_bodies)
        responses = iter([(submit_status, submit_body), *zip(statuses, task_bodies)])
        calls: list[dict] = []
        sleep_delays: list[float] = []

        def fake_request(**kwargs):
            calls.append(kwargs)
            return next(responses)

        self.module.client_module.request = fake_request
        self.module.tasks_module.time.sleep = lambda delay: sleep_delays.append(delay)
        sys.argv = [
            "generate.py",
            "--prompt",
            "fixture prompt that must not be written to the receipt",
            "--output",
            str(target),
            *(extra_args or []),
        ]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.module.main()
        return code, stdout.getvalue(), stderr.getvalue(), calls, sleep_delays

    def test_provider_token_requires_exact_expected_base_url(self) -> None:
        self.write_config(
            'model_provider = "fixture"\n\n[model_providers.fixture]\nexperimental_bearer_token = "fixture-token"\n'
        )
        with self.assertRaisesRegex(self.module.GenerationError, "missing"):
            self.module.provider_token()

        self.write_config(
            'model_provider = "fixture"\n\n[model_providers.fixture]\nbase_url = "https://wrong.example/v1"\nexperimental_bearer_token = "fixture-token"\n'
        )
        with self.assertRaisesRegex(self.module.GenerationError, "wrong.example"):
            self.module.provider_token()

        self.write_config(
            'model_provider = "fixture"\n\n[model_providers.fixture]\nbase_url = "https://api.lsidestudio.com/v1"\nexperimental_bearer_token = "fixture-token"\n'
        )
        self.assertEqual(self.module.provider_token(), "fixture-token")

    def test_async_submission_polls_to_verified_artifact_and_redacted_receipt(self) -> None:
        target = self.root / "artifact.png"
        queued = json.dumps({"status": "processing"})
        code, stdout, stderr, calls, sleep_delays = self.run_main(
            target, self.task_submission(), [queued, self.inline_body(status="completed")]
        )

        resolved_target = target.resolve()
        receipt_path = (self.root / "artifact.png.receipt.json").resolve()
        self.assertEqual(code, 0, stderr)
        self.assertTrue(target.is_file())
        self.assertTrue(receipt_path.is_file())
        self.assertIn(f"saved: {resolved_target}", stdout)
        self.assertIn(f"receipt: {receipt_path}", stdout)
        self.assertEqual(calls[0]["method"], "POST")
        self.assertEqual(calls[0]["url"], self.module.GENERATE_ENDPOINT)
        self.assertEqual(calls[1]["method"], "GET")
        self.assertEqual(calls[1]["url"], self.module.task_url("task-123"))
        self.assertEqual(calls[2]["url"], self.module.task_url("task-123"))
        self.assertEqual(sleep_delays, [self.module.POLL_INTERVAL_SECONDS])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["output"], str(resolved_target))
        self.assertEqual(receipt["format"], "png")
        self.assertEqual(receipt["source"], "inline_base64")
        self.assertFalse(receipt["downloaded"])
        self.assertEqual(receipt["submit_http_status"], 202)
        self.assertEqual(receipt["task_http_status"], 200)
        self.assertEqual(receipt["task_status"], "completed")
        self.assertEqual(receipt["task_polls"], 2)
        self.assertIsInstance(receipt["task_wait_seconds"], float)
        self.assertGreaterEqual(receipt["task_wait_seconds"], 0.0)
        self.assertEqual(len(receipt["sha256"]), 64)
        self.assertNotIn("fixture prompt", receipt_path.read_text(encoding="utf-8"))

    def test_async_edit_uses_edits_endpoint(self) -> None:
        source = self.root / "source.png"
        source.write_bytes(VALID_PNG)
        target = self.root / "edited.png"
        code, _, stderr, calls, _ = self.run_main(
            target,
            self.task_submission(),
            [self.inline_body()],
            extra_args=["--image", str(source)],
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(calls[0]["url"], self.module.EDIT_ENDPOINT)
        self.assertEqual(calls[0]["images"], (source.resolve(),))

    def test_truncated_png_is_rejected_without_output_or_receipt(self) -> None:
        target = self.root / "truncated.png"
        code, stdout, stderr, _, _ = self.run_main(
            target, self.task_submission(), [self.inline_body(b"\x89PNG\r\n\x1a\n")]
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("incomplete", stderr)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "truncated.png.receipt.json").exists())

    def test_url_payload_is_downloaded_and_does_not_store_the_url(self) -> None:
        target = self.root / "downloaded.png"
        signed_url = "https://signed.example/image.png?sig=private-value"
        self.module.download_url = lambda url, destination: destination.write_bytes(VALID_PNG)
        task_body = json.dumps({"result": {"data": [{"url": signed_url}]}})

        code, _, stderr, _, _ = self.run_main(target, self.task_submission(), [task_body])

        self.assertEqual(code, 0, stderr)
        receipt_text = (self.root / "downloaded.png.receipt.json").read_text(encoding="utf-8")
        receipt = json.loads(receipt_text)
        self.assertEqual(receipt["source"], "remote_url")
        self.assertTrue(receipt["downloaded"])
        self.assertNotIn(signed_url, receipt_text)

    def test_json_mode_emits_the_same_machine_readable_receipt(self) -> None:
        target = self.root / "json-artifact.png"
        code, stdout, stderr, _, _ = self.run_main(
            target, self.task_submission(), [self.inline_body()], extra_args=["--json"]
        )

        self.assertEqual(code, 0, stderr)
        emitted = json.loads(stdout)
        saved = json.loads((self.root / "json-artifact.png.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(emitted, saved)
        self.assertEqual(emitted["output"], str(target.resolve()))

    def test_cloudflare_1010_retries_once_and_reuses_the_user_agent_for_polling(self) -> None:
        target = self.root / "retry.png"
        calls: list[dict] = []
        responses = iter(
            [
                (403, "Cloudflare error 1010"),
                (202, self.task_submission()),
                (200, self.inline_body()),
            ]
        )

        def fake_request(**kwargs):
            calls.append(kwargs)
            return next(responses)

        self.module.provider_token = lambda: "fixture-token"
        self.module.client_module.request = fake_request
        self.module.tasks_module.time.sleep = lambda delay: None
        sys.argv = ["generate.py", "--prompt", "fixture", "--output", str(target)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = self.module.main()

        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(
            [call["user_agent"] for call in calls],
            [None, self.module.STANDARD_USER_AGENT, self.module.STANDARD_USER_AGENT],
        )

    def test_failed_task_is_rejected_without_output_or_receipt(self) -> None:
        target = self.root / "failed.png"
        failed = json.dumps({"status": "failed"})
        code, stdout, stderr, _, _ = self.run_main(target, self.task_submission(), [failed])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("task ended with status failed", stderr)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "failed.png.receipt.json").exists())

    def test_task_image_with_pending_status_is_rejected(self) -> None:
        target = self.root / "contradictory.png"
        code, stdout, stderr, _, _ = self.run_main(
            target, self.task_submission(), [self.inline_body(status="processing")]
        )

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("image data with nonterminal status processing", stderr)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "contradictory.png.receipt.json").exists())

    def test_filesystem_errors_are_reported_without_traceback(self) -> None:
        parent_file = self.root / "not-a-directory"
        parent_file.write_text("fixture", encoding="utf-8")
        target = parent_file / "artifact.png"
        code, stdout, stderr, _, _ = self.run_main(target, self.task_submission(), [self.inline_body()])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr.startswith("error: "))
        self.assertNotIn("Traceback", stderr)

    def test_mismatched_output_extension_is_rejected(self) -> None:
        target = self.root / "artifact.webp"
        code, _, stderr, _, _ = self.run_main(target, self.task_submission(), [self.inline_body()])

        self.assertEqual(code, 1)
        self.assertIn("does not match returned png", stderr)
        self.assertFalse(target.exists())

    def test_missing_output_extension_is_rejected(self) -> None:
        target = self.root / "artifact"
        code, _, stderr, _, _ = self.run_main(target, self.task_submission(), [self.inline_body()])

        self.assertEqual(code, 1)
        self.assertIn("no extension", stderr)
        self.assertFalse(target.exists())


    def test_default_model_is_sunburst(self) -> None:
        target = self.root / "default-model.png"
        code, _, stderr, calls, _ = self.run_main(
            target, self.task_submission(), [self.inline_body()]
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(calls[0]["payload"]["model"], "gpt-image-2.5-sunburst")
        receipt = json.loads((self.root / "default-model.png.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["model"], "gpt-image-2.5-sunburst")

    def test_model_override_to_flare(self) -> None:
        target = self.root / "flare-model.png"
        code, _, stderr, calls, _ = self.run_main(
            target,
            self.task_submission(),
            [self.inline_body()],
            extra_args=["--model", "gpt-image-2.5-flare"],
        )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(calls[0]["payload"]["model"], "gpt-image-2.5-flare")
        receipt = json.loads((self.root / "flare-model.png.receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["model"], "gpt-image-2.5-flare")

    def test_edit_form_uses_payload_model(self) -> None:
        response_path = self.root / "resp.json"
        config = self.module.client_module.curl_config(
            "fixture-token",
            response_path,
            None,
            url=self.module.EDIT_ENDPOINT,
            method="POST",
            payload={"model": "gpt-image-2.5-flare", "prompt": "fixture"},
            images=(self.root / "source.png",),
        )

        self.assertIn("model=gpt-image-2.5-flare", config)

    def test_quality_xhigh_and_max_are_accepted(self) -> None:
        for quality in ("xhigh", "max"):
            target = self.root / f"quality-{quality}.png"
            code, _, stderr, calls, _ = self.run_main(
                target,
                self.task_submission(),
                [self.inline_body()],
                extra_args=["--quality", quality],
            )

            self.assertEqual(code, 0, stderr)
            self.assertEqual(calls[0]["payload"]["quality"], quality)

    def test_poll_float_rejects_non_finite_values(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.assertRaises(self.module.argparse.ArgumentTypeError):
                    self.module.positive_float(value)

    def test_request_uses_utf8_encoding_for_curl_stdin(self) -> None:
        temp_dir = self.root / "tmp"
        temp_dir.mkdir()

        class Completed:
            stdout = "200"
            stderr = ""
            returncode = 0

        client = load_fresh_client()
        with (
            mock.patch.object(client.shutil, "which", return_value="/usr/bin/curl"),
            mock.patch.object(client.subprocess, "run", return_value=Completed()) as run,
        ):
            status, body = client.request(
                url=client.GENERATE_ENDPOINT,
                method="POST",
                token="fixture-token",
                temp_dir=temp_dir,
                user_agent=None,
                payload={"model": "gpt-image-2.5-sunburst", "prompt": "中文提示词"},
                images=(Path("/tmp/张三/source image.png"),),
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, "")
        _, kwargs = run.call_args
        self.assertEqual(kwargs.get("encoding"), "utf-8")
        self.assertIn("中文提示词", kwargs.get("input", ""))
        self.assertIn("张三", kwargs.get("input", ""))

    def test_curl_config_preserves_cjk_prompt_and_path(self) -> None:
        response_path = self.root / "resp.json"
        image = Path("/tmp/张三/source image.png")
        config = self.module.client_module.curl_config(
            "fixture-token",
            response_path,
            None,
            url=self.module.GENERATE_ENDPOINT,
            method="POST",
            payload={"model": "gpt-image-2.5-sunburst", "prompt": "中文提示词"},
            images=(image,),
        )

        self.assertIn("中文提示词", config)
        self.assertIn("张三", config)
        self.assertIn("source image.png", config)
        self.assertEqual(config.encode("utf-8").decode("utf-8"), config)

    def test_missing_curl_reports_install_hint(self) -> None:
        client = self.module.client_module
        with mock.patch.object(client.shutil, "which", return_value=None):
            with self.assertRaisesRegex(client.GenerationError, "curl"):
                client.ensure_curl_available()

    def test_percent_encoded_base64_data_url_is_decoded(self) -> None:
        target = self.root / "data-url.png"
        raw = base64.b64encode(VALID_PNG).decode("ascii")
        encoded = "".join(f"%{ord(char):02X}" if index % 3 == 0 else char for index, char in enumerate(raw))
        assert encoded != raw  # the payload must actually exercise percent-decoding
        self.module.client_module.download_url(f"data:image/png;base64,{encoded}", target)

        self.assertEqual(target.read_bytes(), VALID_PNG)


if __name__ == "__main__":
    unittest.main()
