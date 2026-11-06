"""배포 ZIP에 코드만 포함하고 로컬 데이터·가상환경·자격 증명은 제외한다."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def main():
    root = Path(__file__).resolve().parents[1]
    target = root / "dist" / "messenger.zip"
    target.parent.mkdir(exist_ok=True)
    files = [root / "main.py", root / "requirements.txt"]
    for folder in ("app", "frontend"):
        files.extend(path for path in (root / folder).rglob("*") if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc")
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(root))
    print(target)


if __name__ == "__main__":
    main()
