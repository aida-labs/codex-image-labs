from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update.py"


@unittest.skipIf(
    os.environ.get("IMAGE_LABS_UPDATE_CANDIDATE") == "1",
    "updater integration tests are not run inside candidate validation",
)
class UpdateSkillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="image-labs-update-tests-")
        self.root = Path(self.temp.name)
        self.target = self.root / "image-labs"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_update(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            check=False,
        )

    def test_check_does_not_change_existing_non_git_target(self) -> None:
        self.target.mkdir()
        marker = self.target / "old-marker"
        marker.write_text("keep", encoding="utf-8")

        result = self.run_update(
            "--source",
            str(ROOT),
            "--target",
            str(self.target),
            "--check",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertNotIn("client.py", {path.name for path in self.target.iterdir()})
        self.assertIn("target unchanged", result.stdout)

    def test_update_migrates_non_git_target_and_keeps_backup(self) -> None:
        self.target.mkdir()
        marker = self.target / "old-marker"
        marker.write_text("keep", encoding="utf-8")

        result = self.run_update(
            "--source",
            str(ROOT),
            "--target",
            str(self.target),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / "VERSION").is_file())
        self.assertTrue((self.target / "scripts" / "client.py").is_file())
        backups = list((self.root / ".skill-backups").glob("image-labs.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "old-marker").read_text(encoding="utf-8"), "keep")
        self.assertIn("backup:", result.stdout)

    def test_version_reports_non_git_target(self) -> None:
        fixture = self.root / "fixture"
        shutil.copytree(ROOT, fixture, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))

        result = self.run_update("--target", str(fixture), "--version")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("version: 0.1.0", result.stdout)
        self.assertIn("revision: non-git", result.stdout)

    def test_dirty_git_target_is_rejected_without_replacement(self) -> None:
        self.target.mkdir()
        subprocess.run(["git", "-C", str(self.target), "init"], check=True, capture_output=True)
        marker = self.target / "local-change"
        marker.write_text("keep", encoding="utf-8")

        result = self.run_update(
            "--source",
            str(ROOT),
            "--target",
            str(self.target),
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_rollback_restores_backup_and_preserves_current_as_backup(self) -> None:
        self.target.mkdir()
        (self.target / "old-marker").write_text("old", encoding="utf-8")
        update = self.run_update("--source", str(ROOT), "--target", str(self.target))
        self.assertEqual(update.returncode, 0, update.stderr)
        backup = next((self.root / ".skill-backups").glob("image-labs.backup-*"))

        result = self.run_update(
            "--target",
            str(self.target),
            "--rollback",
            str(backup),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.target / "old-marker").read_text(encoding="utf-8"), "old")
        displaced = [
            path for path in (self.root / ".skill-backups").glob("image-labs.backup-*")
            if path != backup
        ]
        self.assertEqual(len(displaced), 1)
        self.assertTrue((displaced[0] / "scripts" / "generate.py").is_file())

    def test_skills_directory_uses_backup_root_outside_skill_scan_path(self) -> None:
        skills_root = self.root / "skills"
        self.target = skills_root / "image-labs"
        self.target.mkdir(parents=True)
        (self.target / "old-marker").write_text("old", encoding="utf-8")

        result = self.run_update(
            "--source",
            str(ROOT),
            "--target",
            str(self.target),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "skill-backups").is_dir())
        self.assertEqual(list(skills_root.glob("image-labs.backup-*")), [])
        self.assertEqual(len(list((self.root / "skill-backups").glob("image-labs.backup-*"))), 1)


if __name__ == "__main__":
    unittest.main()
