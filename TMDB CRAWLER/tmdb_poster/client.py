from __future__ import annotations

import json
import re
import socket
import time
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TMDBError(Exception):
    """사용자에게 표시해도 되는 오류. 인증 키나 요청 URL을 포함하지 않습니다."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Work:
    id: int
    media_type: str
    korean_title: str
    original_title: str
    year: str
    original_language: str = ""
    genre_ids: tuple[int, ...] = ()

    @property
    def label(self) -> str:
        kind = "TV" if self.media_type == "tv" else "영화"
        return f"{self.korean_title} ({self.year or '연도 미상'} · {kind}) / {self.original_title}"

    @property
    def has_korean_title(self) -> bool:
        return any("가" <= char <= "힣" for char in self.korean_title)


@dataclass(frozen=True)
class Poster:
    file_path: str
    language: str | None
    width: int
    height: int
    vote_average: float = 0.0
    vote_count: int = 0


def normalized_query(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).replace("_", " ").split())


class TMDBClient:
    API_BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str, timeout: float = 15.0, retries: int = 2):
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.retries = retries
        self._image_base: str | None = None
        self._poster_sizes: list[str] = []

    def _read(self, url: str, *, image: bool = False) -> bytes:
        headers = {"User-Agent": "TMDB-Poster-Downloader/1.0"}
        headers["Accept"] = "image/*" if image else "application/json"
        for attempt in range(self.retries + 1):
            try:
                with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                    data = response.read()
                    if image and not response.headers.get("Content-Type", "").lower().startswith("image/"):
                        raise TMDBError("이미지 서버에서 올바른 이미지가 오지 않았습니다.")
                    return data
            except HTTPError as error:
                code = error.code
                retry_after = error.headers.get("Retry-After", "")
                error.close()
                if code in (401, 403):
                    raise TMDBError("TMDB 인증에 실패했습니다. API 키를 확인해 주세요.", status_code=code) from None
                if code == 404:
                    raise TMDBError("작품 또는 이미지를 찾지 못했습니다.", status_code=code) from None
                if (code == 429 or code >= 500) and attempt < self.retries:
                    delay = min(float(retry_after), 10) if retry_after.isdigit() else 1 + attempt
                    time.sleep(delay)
                    continue
                if code == 429:
                    raise TMDBError("TMDB 요청이 많습니다. 잠시 후 다시 시도해 주세요.", status_code=code) from None
                raise TMDBError(f"TMDB 서버 오류가 발생했습니다. (HTTP {code})", status_code=code) from None
            except (URLError, TimeoutError, socket.timeout, OSError):
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise TMDBError("서버에 연결하지 못했습니다. 인터넷 연결을 확인해 주세요.") from None
        raise TMDBError("요청을 완료하지 못했습니다.")

    def _json(self, endpoint: str, **params: Any) -> dict:
        if not self.api_key:
            raise TMDBError("먼저 TMDB API 키를 입력해 주세요.")
        params["api_key"] = self.api_key
        try:
            result = json.loads(self._read(f"{self.API_BASE}{endpoint}?{urlencode(params)}"))
        except (ValueError, UnicodeError):
            raise TMDBError("TMDB 응답을 읽지 못했습니다. 다시 시도해 주세요.") from None
        if not isinstance(result, dict):
            raise TMDBError("TMDB 응답 형식이 올바르지 않습니다.")
        return result

    def search(self, query: str, media_type: str = "tv") -> list[Work]:
        query = normalized_query(query)
        if not query:
            raise TMDBError("검색할 작품 제목을 입력해 주세요.")
        if media_type not in ("tv", "movie", "multi"):
            raise ValueError("지원하지 않는 작품 유형입니다.")
        # ko-KR 응답으로 원제 검색 결과의 한국어 제목까지 함께 받습니다.
        result = self._json(f"/search/{media_type}", query=query, language="ko-KR", include_adult="false", page=1)
        works = []
        for item in result.get("results", []):
            kind = item.get("media_type", media_type)
            if kind not in ("tv", "movie"):
                continue
            works.append(Work(
                id=int(item["id"]), media_type=kind,
                korean_title=item.get("name") or item.get("title") or "제목 없음",
                original_title=item.get("original_name") or item.get("original_title") or "",
                year=(item.get("first_air_date") or item.get("release_date") or "")[:4],
                original_language=item.get("original_language", ""),
                genre_ids=tuple(item.get("genre_ids", [])),
            ))
        # 정확하게 일치하는 원제/번역 제목을 먼저 보여주며, 모호한 결과는 사용자가 고릅니다.
        works.sort(key=lambda work: normalized_query(query).casefold() not in
                   (normalized_query(work.original_title).casefold(), normalized_query(work.korean_title).casefold()))
        return works

    def search_anime(self, query: str, media_type: str = "multi") -> list[Work]:
        works = [work for work in self.search(query, media_type) if 16 in work.genre_ids]
        if not works:
            # TV의 여러 시즌은 TMDB에서 한 작품으로 묶일 수 있어 제목 끝의 시즌 표기를 재시도합니다.
            simpler = re.sub(r"\s*(?:제?\s*\d+\s*기|시즌\s*\d+|\d+\s*시즌|season\s*\d+|part\s*\d+)\s*$", "", normalized_query(query), flags=re.I).strip()
            if simpler and simpler != normalized_query(query):
                works = [work for work in self.search(simpler, media_type) if 16 in work.genre_ids]
        works.sort(key=lambda work: work.original_language != "ja")
        return works

    def posters(self, work: Work) -> list[Poster]:
        # API 필터와 로컬 필터를 함께 적용해 일본어 등록 포스터만 반환합니다.
        result = self._json(f"/{work.media_type}/{work.id}/images", language="ja", include_image_language="ja")
        unique: dict[str, Poster] = {}
        for item in result.get("posters", []):
            path = item.get("file_path")
            if path and path.startswith("/") and item.get("iso_639_1") == "ja":
                unique[path] = Poster(
                    file_path=path, language=item.get("iso_639_1"),
                    width=int(item.get("width", 0)), height=int(item.get("height", 0)),
                    vote_average=float(item.get("vote_average", 0)), vote_count=int(item.get("vote_count", 0)),
                )
        return sorted(unique.values(), key=lambda poster: (
            -poster.vote_average, -poster.vote_count, -poster.width,
        ))

    def configure_images(self) -> None:
        if self._image_base is not None:
            return
        config = self._json("/configuration").get("images", {})
        base = config.get("secure_base_url", "")
        if not base.startswith("https://"):
            raise TMDBError("TMDB 이미지 서버 설정을 읽지 못했습니다.")
        self._image_base = base.rstrip("/") + "/"
        self._poster_sizes = config.get("poster_sizes", [])

    def image_url(self, poster: Poster, *, original: bool = False) -> str:
        self.configure_images()
        size = "original" if original else next(
            (size for size in ("w185", "w342", "w500", "original") if size in self._poster_sizes), "original"
        )
        if not poster.file_path.startswith("/") or ".." in poster.file_path or "?" in poster.file_path:
            raise TMDBError("올바르지 않은 이미지 경로입니다.")
        return f"{self._image_base}{size}{poster.file_path}"

    def image_bytes(self, poster: Poster, *, original: bool = False) -> bytes:
        return self._read(self.image_url(poster, original=original), image=True)
