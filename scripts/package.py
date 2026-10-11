"""Build a plugin ZIP without runtime state, credentials or development dependencies."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / "dist" / "maibot-bilibili-dynamic-0.1.0.zip"
    target.parent.mkdir(exist_ok=True)
    files = [
        ROOT / name
        for name in (
            "plugin.py",
            "_manifest.json",
            "README.md",
            "LICENSE",
            "NOTICE",
            "config.example.toml",
            "pyproject.toml",
            "uv.lock",
            "main.py",
        )
    ]
    files += sorted((ROOT / "bili").glob("*.py"))
    files += sorted((ROOT / "tests").glob("*.py"))
    files += sorted((ROOT / "scripts").glob("*.py"))
    files += [ROOT / "docs" / "card-preview.png"]
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "maibot-bilibili-dynamic/" + path.relative_to(ROOT).as_posix())
    print(target)


if __name__ == "__main__":
    main()
