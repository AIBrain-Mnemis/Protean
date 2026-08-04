"""Configure Protean storage during setup."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, set_key


@dataclass(frozen=True)
class StoragePaths:
    data: Path
    skills: Path
    recordings: Path


@dataclass(frozen=True)
class MigrationItem:
    source: Path
    destination: Path
    kind: str


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--prompt-migration", action="store_true")
    args = parser.parse_args()

    repo_root = args.repo_root.expanduser().resolve()
    env_file = args.env_file.expanduser().resolve()
    if not args.data_dir.expanduser().is_absolute():
        parser.error("--data-dir must be an absolute path")
    target_data = args.data_dir.expanduser().resolve()
    target = StoragePaths(
        data=target_data,
        skills=target_data / "skills",
        recordings=target_data / "recordings",
    )
    source = _current_paths(repo_root, env_file)
    items = _migration_items(source, target)
    _validate_destinations(items)

    if items and args.prompt_migration and not _confirm_migration(items, target.data):
        print("Storage paths and existing data were left unchanged.")
        raise SystemExit(2)

    _move_items(items, target, env_file)
    counts = {
        kind: sum(item.kind == kind for item in items)
        for kind in ("data", "skill", "recording")
    }
    print(f"Storage configured: {target.data}")
    print(
        "Moved: "
        f"{counts['data']} data item(s), "
        f"{counts['skill']} skill(s), "
        f"{counts['recording']} recording(s)"
    )


def _confirm_migration(items: list[MigrationItem], target: Path) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        reply = input(
            f"Move {len(items)} existing Protean item(s) to {target}? [y/N] "
        )
    except EOFError:
        return False
    return reply.strip().casefold() in {"y", "yes"}


def _move_items(items: list[MigrationItem], target: StoragePaths, env_file: Path) -> None:
    moved: list[MigrationItem] = []
    try:
        for item in items:
            item.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item.source), str(item.destination))
            moved.append(item)
        target.data.mkdir(parents=True, exist_ok=True)
        target.skills.mkdir(parents=True, exist_ok=True)
        target.recordings.mkdir(parents=True, exist_ok=True)
        _write_storage_env(env_file, target)
    except Exception:
        for item in reversed(moved):
            if item.destination.exists() and not item.source.exists():
                item.source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item.destination), str(item.source))
        raise


def _current_paths(repo_root: Path, env_file: Path) -> StoragePaths:
    values = dotenv_values(env_file) if env_file.exists() else {}
    default_data = repo_root / "data"
    return StoragePaths(
        data=_configured_path(values, "PROTEAN_DATA_DIR", default_data, repo_root),
        skills=_configured_path(
            values, "PROTEAN_SKILLS_DIR", default_data / "skills", repo_root
        ),
        recordings=_configured_path(
            values, "PROTEAN_RECORDINGS_DIR", default_data / "recordings", repo_root
        ),
    )


def _configured_path(
    values: dict[str, str | None],
    key: str,
    default: Path,
    repo_root: Path,
) -> Path:
    if key not in values:
        return default.resolve()
    raw = values[key]
    if raw is None or not raw.strip():
        return repo_root.resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve()


def _migration_items(source: StoragePaths, target: StoragePaths) -> list[MigrationItem]:
    items: list[MigrationItem] = []
    if source.data != target.data:
        for name in ("trajectory_markers.jsonl", "telemetry"):
            path = source.data / name
            if path.exists():
                items.append(MigrationItem(path, target.data / name, "data"))

    if source.skills != target.skills and source.skills.is_dir():
        for candidate in sorted(source.skills.iterdir()):
            if candidate.is_dir() and (candidate / "SKILL.md").is_file():
                items.append(MigrationItem(candidate, target.skills / candidate.name, "skill"))

    if source.recordings != target.recordings and source.recordings.is_dir():
        for candidate in sorted(source.recordings.glob("rec-*")):
            if candidate.is_dir() and _is_recording(candidate):
                items.append(
                    MigrationItem(candidate, target.recordings / candidate.name, "recording")
                )
    return items


def _is_recording(path: Path) -> bool:
    return any(
        (path / name).is_file()
        for name in ("events.json", "events.journal.jsonl", "recording.mov")
    )


def _validate_destinations(items: list[MigrationItem]) -> None:
    destinations: set[Path] = set()
    for item in items:
        if item.destination in destinations or item.destination.exists():
            raise FileExistsError(f"Migration destination already exists: {item.destination}")
        destinations.add(item.destination)


def _write_storage_env(env_file: Path, paths: StoragePaths) -> None:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    temp = env_file.with_name(f".{env_file.name}.protean-{uuid.uuid4().hex}")
    if env_file.exists():
        shutil.copy2(env_file, temp)
    else:
        temp.touch()
    try:
        for key, value in (
            ("PROTEAN_DATA_DIR", paths.data),
            ("PROTEAN_SKILLS_DIR", paths.skills),
            ("PROTEAN_RECORDINGS_DIR", paths.recordings),
        ):
            set_key(str(temp), key, value.as_posix(), quote_mode="auto")
        os.replace(temp, env_file)
    finally:
        if temp.exists():
            temp.unlink()


if __name__ == "__main__":
    main()
