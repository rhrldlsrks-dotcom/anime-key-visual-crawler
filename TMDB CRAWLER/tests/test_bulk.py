from __future__ import annotations

import csv
import json
import tempfile
import threading
import unittest
from pathlib import Path

from test_core import sample_image
from tmdb_poster.bulk import download_first_posters
from tmdb_poster.client import Poster, TMDBError, Work
from tmdb_poster.storage import save_image
from tmdb_poster.workbook import ExcelTitle, WorkbookTitles


class BulkClient:
    def __init__(self):
        self.searches = []
        self.downloads = []
        self.failing = set()
        self.failure_code = None
        self.stop_on_image = None

    def search_anime(self, title):
        self.searches.append(title)
        if title in self.failing:
            raise TMDBError("테스트 서버 오류", status_code=self.failure_code)
        if title == "미등록":
            return []
        return [Work(len(self.searches), "tv", title, title, "2023", "ja", (16,))]

    def posters(self, work):
        if work.korean_title == "포스터없음":
            return [Poster("/english.jpg", "en", 300, 450)]
        return [Poster(f"/first-{work.id}.jpg", "ja", 300, 450), Poster(f"/second-{work.id}.jpg", "ja", 300, 450)]

    def configure_images(self):
        pass

    def image_bytes(self, poster, *, original=False):
        self.downloads.append((poster.file_path, original))
        if self.stop_on_image:
            self.stop_on_image.set()
        return sample_image()


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve()
        self.client = BulkClient()

    def tearDown(self):
        self.tmp.cleanup()

    def workbook(self, titles):
        return WorkbookTitles(self.directory / "원본.xlsx", "목록", [ExcelTitle(row, title) for row, title in enumerate(titles, 2)])

    def run_bulk(self, titles, stop=None, callback=lambda update: None):
        return download_first_posters(self.workbook(titles), self.client, self.directory,
                                      stop or threading.Event(), callback, interval=0)

    def test_all_rows_skip_existing_and_duplicate_titles_and_only_download_first(self):
        existing = save_image(sample_image(), self.directory, "이미 저장")
        before = existing.read_bytes()
        stats = self.run_bulk(["이미_저장", "첫_제목", "다른 제목", "첫 제목"])
        self.assertEqual((stats.total, stats.processed, stats.saved, stats.skipped), (4, 4, 2, 2))
        self.assertFalse(stats.stopped)
        self.assertEqual(self.client.searches, ["첫_제목", "다른 제목"])
        self.assertTrue(all(path.startswith("/first-") and original for path, original in self.client.downloads))
        self.assertEqual(existing.read_bytes(), before)
        self.assertTrue((self.directory / "첫 제목.jpg").is_file())
        self.assertFalse(list(self.directory.glob("* (2).jpg")))
        records = [json.loads(line) for line in (self.directory / "download_history.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["source"]["row"] for record in records], [3, 4])
        self.assertTrue(all(record["mode"] == "bulk_first_poster" for record in records))
        with stats.report.open(encoding="utf-8-sig", newline="") as report:
            self.assertEqual(len(list(csv.DictReader(report))), 4)

    def test_missing_work_or_japanese_poster_is_reported_then_continues(self):
        stats = self.run_bulk(["미등록", "포스터없음", "정상 제목"])
        self.assertEqual((stats.processed, stats.saved, stats.missing, stats.failed), (3, 1, 2, 0))
        self.assertEqual(len(self.client.downloads), 1)
        self.assertTrue((self.directory / "정상 제목.jpg").is_file())
        self.assertIn("일본어 포스터 없음", stats.report.read_text(encoding="utf-8-sig"))

    def test_stop_after_first_then_rerun_keeps_saved_files(self):
        stop = threading.Event()
        stats = self.run_bulk(["첫 제목", "둘째 제목", "셋째 제목"], stop,
                              lambda update: stop.set() if update.state == "saved" else None)
        self.assertTrue(stats.stopped)
        self.assertEqual((stats.processed, stats.saved), (1, 1))
        before = (self.directory / "첫 제목.jpg").read_bytes()
        resumed = self.run_bulk(["첫 제목", "둘째 제목", "셋째 제목"])
        self.assertEqual((resumed.saved, resumed.skipped), (2, 1))
        self.assertEqual((self.directory / "첫 제목.jpg").read_bytes(), before)
        self.assertNotEqual(stats.report, resumed.report)
        self.assertFalse(list(self.directory.glob("* (2).jpg")))

    def test_stop_during_request_does_not_write_the_downloaded_image(self):
        stop = threading.Event()
        self.client.stop_on_image = stop
        stats = self.run_bulk(["첫 제목", "다음 제목"], stop)
        self.assertTrue(stats.stopped)
        self.assertEqual(stats.saved, 0)
        self.assertEqual(stats.processed, 0)
        self.assertFalse(list(self.directory.glob("*.jpg")))

    def test_one_server_error_does_not_block_other_titles(self):
        self.client.failing = {"오류 제목"}
        stats = self.run_bulk(["오류 제목", "정상 제목"])
        self.assertEqual((stats.failed, stats.saved), (1, 1))
        self.assertFalse(stats.stopped)
        self.assertIn("테스트 서버 오류", stats.report.read_text(encoding="utf-8-sig"))

    def test_bad_credentials_or_rate_limit_stop_before_repeating_entire_workbook(self):
        for code in (401, 429):
            with self.subTest(code=code):
                self.client = BulkClient()
                self.client.failing = {"첫 제목"}
                self.client.failure_code = code
                stats = self.run_bulk(["첫 제목", "둘째 제목", "셋째 제목"])
                self.assertTrue(stats.stopped)
                self.assertEqual(stats.failed, 1)
                self.assertEqual(len(self.client.searches), 1)
                self.assertTrue(stats.halt_reason)

    def test_repeated_connection_failures_stop_after_three(self):
        titles = ["첫 오류", "둘째 오류", "셋째 오류", "아직 안 할 제목"]
        self.client.failing = set(titles)
        stats = self.run_bulk(titles)
        self.assertTrue(stats.stopped)
        self.assertEqual(stats.failed, 3)
        self.assertEqual(len(self.client.searches), 3)


if __name__ == "__main__":
    unittest.main()
