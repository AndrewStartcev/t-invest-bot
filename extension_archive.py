"""Build an installable *unpacked* Brave/Chromium extension archive on demand.

The ZIP is assembled from a fixed allowlist. No server configuration, cookie,
Telegram token, pairing key, broker credential or other local file is included.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ZIP_NAME = "t-invest-pulse-extension.zip"
ZIP_ROOT = "t-invest-pulse-extension"
EXTENSION_FILES = (
    "manifest.json",
    "background.js",
    "content.js",
    "portfolio.js",
    "popup.html",
    "popup.css",
    "popup.js",
    "README.md",
)


def extension_zip(project_root: str | Path) -> bytes:
    folder = Path(project_root) / "extension"
    result = BytesIO()
    with ZipFile(result, mode="w", compression=ZIP_DEFLATED) as archive:
        for filename in EXTENSION_FILES:
            file_path = folder / filename
            if not file_path.is_file() or file_path.is_symlink():
                raise FileNotFoundError("Не найден файл расширения: " + filename)
            archive.write(file_path, arcname=f"{ZIP_ROOT}/{filename}")
    return result.getvalue()
