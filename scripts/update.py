#!/usr/bin/env python3
"""Safely install or update the image-labs skill."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_URL = "https://github.com/aida-labs/codex-image-labs.git"
REQUIRED_PATHS = ("SKILL.md", "VERSION", "scripts/generate.py", "tests")


class UpdateError(RuntimeError):
    pass


def skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_version(root: Path) -> str:
    version_path = root / "VERSION"
    if not version_path.is_file():
        return "unknown"
    version = version_path.read_text(encoding="utf-8").strip()
    return version or "unknown"


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise UpdateError(detail)
    return result.stdout.strip()


def current_revision(root: Path) -> str:
    if not (root / ".git").exists():
        return "non-git"
    try:
        return git_output(root, "rev-parse", "--short", "HEAD")
    except UpdateError:
        return "git-unknown"


def ensure_required_paths(root: Path) -> None:
    missing = [path for path in REQUIRED_PATHS if not (root / path).exists()]
    if missing:
        raise UpdateError(f"Candidate is missing required paths: {', '.join(missing)}")


def run_tests(root: Path) -> None:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["IMAGE_LABS_UPDATE_CANDIDATE"] = "1"
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=root,
        env=env,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise UpdateError(f"Candidate regression suite failed with exit code {result.returncode}")


def copy_local_source(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise UpdateError(f"Local source directory not found: {source}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "outputs", "*.receipt.json"),
    )


def prepare_candidate(source: Path | None, repository: str, parent: Path) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=f".{parent.name}.candidate-", dir=parent))
    staging.rmdir()
    try:
        if source is not None:
            copy_local_source(source.expanduser().resolve(), staging)
        else:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", repository, str(staging)],
                text=True,
                encoding="utf-8",
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or "git clone failed"
                raise UpdateError(detail)
        ensure_required_paths(staging)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def target_is_dirty(target: Path) -> bool:
    if not (target / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(target), "status", "--porcelain"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise UpdateError(result.stderr.strip() or "cannot inspect target Git status")
    return bool(result.stdout.strip())


def backup_path(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = target.with_name(f"{target.name}.backup-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = target.with_name(f"{target.name}.backup-{stamp}-{suffix}")
        suffix += 1
    return candidate


def install_candidate(candidate: Path, target: Path) -> Path | None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = backup_path(target)
        target.replace(backup)
    try:
        candidate.replace(target)
    except Exception:
        if backup is not None and not target.exists():
            backup.replace(target)
        raise
    return backup


def rollback(target: Path, backup: Path) -> None:
    if not backup.is_dir():
        raise UpdateError(f"Backup directory not found: {backup}")
    if target.exists():
        displaced = backup_path(target)
        target.replace(displaced)
    backup.replace(target)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Safely install or update the image-labs skill.")
    parser.add_argument(
        "--target",
        type=Path,
        default=skill_root(),
        help="Skill directory to update; defaults to the directory containing this script",
    )
    parser.add_argument(
        "--repository",
        default=REPOSITORY_URL,
        help="Git repository to clone when --source is not provided",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Local skill directory used instead of cloning; intended for tests or offline staging",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Prepare and test an update without replacing the target",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the target version and revision, then exit",
    )
    parser.add_argument(
        "--rollback",
        type=Path,
        help="Restore a previously printed backup directory instead of updating",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    target = args.target.expanduser().resolve()
    if args.version:
        print(f"version: {read_version(target)}")
        print(f"revision: {current_revision(target)}")
        return 0
    if args.rollback:
        try:
            rollback(target, args.rollback.expanduser().resolve())
            print(f"rolled_back: {target}")
            print(f"current_version: {read_version(target)}")
            print(f"current_revision: {current_revision(target)}")
            return 0
        except (OSError, UpdateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    candidate: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target_is_dirty(target):
            raise UpdateError(
                f"Target has uncommitted changes; back them up or commit them before updating: {target}"
            )
        candidate = prepare_candidate(args.source, args.repository, target.parent)
        print(f"candidate_version: {read_version(candidate)}")
        print(f"candidate_revision: {current_revision(candidate)}")
        print("running_tests: yes")
        run_tests(candidate)
        if args.check:
            print("check: passed; target unchanged")
            return 0

        previous_version = read_version(target) if target.exists() else "none"
        previous_revision = current_revision(target) if target.exists() else "none"
        backup = install_candidate(candidate, target)
        candidate = None
        print(f"updated: {target}")
        print(f"previous_version: {previous_version}")
        print(f"previous_revision: {previous_revision}")
        print(f"current_version: {read_version(target)}")
        print(f"current_revision: {current_revision(target)}")
        print(f"backup: {backup or 'none'}")
        return 0
    except (OSError, UpdateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if candidate is not None:
            shutil.rmtree(candidate, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
