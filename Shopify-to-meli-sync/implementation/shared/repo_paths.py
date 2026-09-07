from pathlib import Path


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path(__file__)).resolve()
    if current.is_file():
        current = current.parent

    for path in (current, *current.parents):
        if (path / ".git").exists() or (path / "openspec").exists():
            return path

    return Path.cwd().resolve()


REPO_ROOT = find_repo_root()
