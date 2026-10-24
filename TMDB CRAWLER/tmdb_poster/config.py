from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUERY = "薬屋のひとりごと"
DEFAULT_KOREAN_TITLE = "약사의 혼잣말"
DEFAULT_EXCEL = Path(r"C:\Users\houshou marine\Desktop\나의 투쟁\anilog\애니로그-전체-2026-10-12.xlsx")


def load_api_key(path: Path = ROOT / ".env") -> str:
    if key := os.environ.get("TMDB_API_KEY", "").strip():
        return key
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "TMDB_API_KEY":
                return value.strip().strip("\"'")
    return ""


def save_api_key(key: str, path: Path = ROOT / ".env") -> None:
    key = key.strip()
    if not key or "\n" in key or "\r" in key:
        raise ValueError("API 키를 확인해 주세요.")
    path.write_text(f"TMDB_API_KEY={key}\n", encoding="utf-8")
