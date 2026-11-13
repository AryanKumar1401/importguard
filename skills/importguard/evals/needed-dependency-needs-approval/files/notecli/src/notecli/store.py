import json
import os
from pathlib import Path


def store_path() -> Path:
    return Path(os.environ.get("NOTECLI_HOME", Path.home() / ".notecli")) / "notes.json"


def load() -> list[str]:
    path = store_path()
    if not path.exists():
        return []
    return json.loads(path.read_text())


def save(notes: list[str]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(notes))
