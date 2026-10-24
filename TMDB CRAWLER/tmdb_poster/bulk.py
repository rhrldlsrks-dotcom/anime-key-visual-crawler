from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .client import Poster, TMDBClient, TMDBError, Work
from .storage import AlreadySaved, SavedPosters, history_record, safe_title, save_image
from .workbook import ExcelTitle, WorkbookTitles


@dataclass
class BulkSummary:
    total: int
    processed: int = 0
    saved: int = 0
    skipped: int = 0
    missing: int = 0
    failed: int = 0
    stopped: bool = False
    halt_reason: str = ""
    report: Path | None = None


@dataclass(frozen=True)
class BulkUpdate:
    index: int
    item: ExcelTitle
    state: str
    stats: BulkSummary
    path: Path | None = None
    work: Work | None = None
    poster: Poster | None = None
    message: str = ""


def _open_report(directory: Path):
    stem = f"bulk_results_{datetime.now():%Y%m%d_%H%M%S}"
    number = 1
    while True:
        path = directory / f"{stem}{'' if number == 1 else f' ({number})'}.csv"
        try:
            return path, path.open("x", encoding="utf-8-sig", newline="")
        except FileExistsError:
            number += 1


def _error_message(error: Exception) -> str:
    if isinstance(error, (TMDBError, ValueError)):
        return str(error)
    if isinstance(error, OSError):
        return "파일을 저장하지 못했습니다. 폴더 권한과 디스크 공간을 확인해 주세요."
    return "작품 처리 중 오류가 발생했습니다. 이 제목은 수동으로 다시 확인해 주세요."


def download_first_posters(workbook: WorkbookTitles, client: TMDBClient, directory: Path,
                           stop: threading.Event, on_update: Callable[[BulkUpdate], None],
                           *, interval: float = 0.2) -> BulkSummary:
    """전체 A열에서 미저장 제목만 처리하며 UI와 동일한 첫 일본어 포스터를 원본으로 저장합니다."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    existing = SavedPosters(directory)
    stats = BulkSummary(total=len(workbook.items))
    stats.report, output = _open_report(directory)
    consecutive_errors = 0
    work_cache = {}
    poster_cache = {}
    labels = {"saved": "저장 완료", "exists": "이미 저장됨", "not_found": "작품 검색 결과 없음",
              "no_posters": "일본어 포스터 없음", "error": "실패"}
    with output:
        writer = csv.writer(output)
        writer.writerow(["엑셀 행", "원래 제목", "상태", "저장 파일", "TMDB 종류", "TMDB ID", "내용"])
        output.flush()
        for index, item in enumerate(workbook.items):
            if stop.is_set():
                break
            path = existing.find(item.title)
            work = poster = None
            message = ""
            fatal = False
            state = "exists"
            if path is None:
                on_update(BulkUpdate(index, item, "searching", replace(stats)))
                try:
                    safe_title(item.title)
                    if item.title not in work_cache:
                        work_cache[item.title] = client.search_anime(item.title)
                    if stop.is_set():
                        break
                    works = work_cache[item.title]
                    if not works:
                        state = "not_found"
                    else:
                        work = works[0]
                        key = (work.media_type, work.id)
                        if key not in poster_cache:
                            poster_cache[key] = [poster for poster in client.posters(work) if poster.language == "ja"]
                        if stop.is_set():
                            break
                        posters = poster_cache[key]
                        if not posters:
                            state = "no_posters"
                        else:
                            poster = posters[0]
                            client.configure_images()
                            if stop.is_set():
                                break
                            data = client.image_bytes(poster, original=True)
                            if stop.is_set():
                                break
                            path = save_image(data, directory, item.title, skip_existing=True)
                            existing.add(path)
                            state = "saved"
                            record = history_record(work, poster, path, item.title)
                            record["source"] = {"excel_path": str(workbook.path), "sheet": workbook.sheet, "row": item.row}
                            record["mode"] = "bulk_first_poster"
                            try:
                                with (directory / "download_history.jsonl").open("a", encoding="utf-8") as history:
                                    history.write(json.dumps(record, ensure_ascii=False) + "\n")
                            except OSError:
                                message = "이미지 저장 완료 · 다운로드 기록 저장 실패"
                    consecutive_errors = 0
                except AlreadySaved as error:
                    state, path = "exists", error.path
                    existing.add(path)
                    consecutive_errors = 0
                except Exception as error:
                    if stop.is_set():
                        break
                    state, message = "error", _error_message(error)
                    consecutive_errors += 1
                    fatal = isinstance(error, OSError) or (
                        isinstance(error, TMDBError) and error.status_code in (401, 403, 429)
                    ) or consecutive_errors >= 3
                    if fatal:
                        stats.halt_reason = message
            else:
                consecutive_errors = 0
            stats.processed += 1
            if state == "saved":
                stats.saved += 1
            elif state == "exists":
                stats.skipped += 1
            elif state in ("not_found", "no_posters"):
                stats.missing += 1
            else:
                stats.failed += 1
            writer.writerow([item.row, item.title, labels[state], path.name if path else "",
                             work.media_type if work else "", work.id if work else "", message])
            output.flush()
            on_update(BulkUpdate(index, item, state, replace(stats), path, work, poster, message))
            if fatal:
                break
            if state != "exists" and interval:
                stop.wait(interval)
    stats.stopped = stats.processed < stats.total
    return stats
