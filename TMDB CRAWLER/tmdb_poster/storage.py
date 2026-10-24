from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .client import Poster, TMDBClient, TMDBError, Work


class AlreadySaved(Exception):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"이미 저장된 제목입니다: {path.name}")


class SavedPosters:
    """선택한 저장 폴더의 실제 이미지로 중복을 판단합니다. 삭제된 파일은 저장 완료로 보지 않습니다."""

    def __init__(self, directory: Path):
        self.directory = Path(directory).expanduser().resolve()
        self.files: dict[str, Path] = {}
        self.refresh()

    def refresh(self):
        self.files = {}
        if self.directory.is_dir():
            for path in self.directory.iterdir():
                if path.is_file() and path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                    try:
                        self.files.setdefault(safe_title(path.stem).casefold(), path)
                    except ValueError:
                        continue

    def find(self, title: str) -> Path | None:
        try:
            key = safe_title(title).casefold()
        except ValueError:
            return None
        if (path := self.files.get(key)) and path.is_file():
            return path
        for stem, path in self.files.items():
            if stem.startswith(key + " (") and re.fullmatch(re.escape(key) + r" \([1-9][0-9]*\)", stem) and path.is_file():
                return path
        return None

    def add(self, path: Path):
        self.files[safe_title(path.stem).casefold()] = path


def safe_title(title: str) -> str:
    """엑셀의 원래 한글 제목을 그대로 받으며 밑줄은 공백으로 바꿉니다."""
    title = unicodedata.normalize("NFC", title).replace("_", " ")
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", title)
    title = re.sub(r"\s+", " ", title).strip(" .")
    title = title[:100].rstrip(" .")
    if not title:
        raise ValueError("저장할 한글 제목을 입력해 주세요.")
    # Windows 예약 장치명은 확장자가 붙어도 파일명으로 쓸 수 없습니다.
    stem = title.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        title = f"작품 {title}"
    return title


def save_image(data: bytes, directory: Path, title: str, *, skip_existing: bool = False) -> Path:
    """검증한 원본 바이트를 저장. 기존 파일을 덮어쓰지 않고 공백+번호를 붙입니다."""
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise TMDBError("다운로드한 이미지가 손상되었습니다. 다른 포스터를 선택해 주세요.") from None
    extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image_format)
    if not extension:
        raise TMDBError("지원하지 않는 포스터 이미지 형식입니다.")
    name = safe_title(title)
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if skip_existing and (existing := SavedPosters(directory).find(title)):
        raise AlreadySaved(existing)
    index = 1
    while True:
        suffix = "" if index == 1 else f" ({index})"
        target = directory / f"{name}{suffix}{extension}"
        try:
            output = target.open("xb")
        except FileExistsError:
            if skip_existing:
                raise AlreadySaved(target) from None
            index += 1
            continue
        try:
            with output:
                output.write(data)
        except OSError:
            target.unlink(missing_ok=True)
            raise
        return target


def download_poster(client: TMDBClient, work: Work, poster: Poster, directory: Path, original_korean_title: str,
                    *, skip_existing: bool = True) -> Path:
    safe_title(original_korean_title)
    if skip_existing and (existing := SavedPosters(directory).find(original_korean_title)):
        raise AlreadySaved(existing)
    data = client.image_bytes(poster, original=True)
    return save_image(data, directory, original_korean_title, skip_existing=skip_existing)


def history_record(work: Work, poster: Poster, path: Path, original_korean_title: str) -> dict:
    return {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "original_korean_title": original_korean_title,
        "file_name": path.name,
        "path": str(path),
        "work": asdict(work),
        "poster": asdict(poster),
    }
