from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from PIL import Image

from tmdb_poster.client import Poster, TMDBClient, TMDBError, Work
from tmdb_poster.storage import AlreadySaved, SavedPosters, download_poster, safe_title, save_image


def sample_image() -> bytes:
    output = BytesIO()
    Image.new("RGB", (300, 450), "#234567").save(output, format="JPEG")
    return output.getvalue()


class StorageTests(unittest.TestCase):
    def test_korean_title_underscores_and_windows_characters(self):
        self.assertEqual(safe_title("  약사의__혼잣말:  2기/일본판. "), "약사의 혼잣말 2기 일본판")
        self.assertEqual(safe_title("CON"), "작품 CON")
        with self.assertRaises(ValueError):
            safe_title("_ / : ")

    def test_duplicate_keeps_original_bytes_and_uses_real_extension(self):
        data = sample_image()
        with tempfile.TemporaryDirectory() as tmp:
            first = save_image(data, Path(tmp), "약사의_혼잣말")
            second = save_image(data, Path(tmp), "약사의_혼잣말")
            self.assertEqual(first.name, "약사의 혼잣말.jpg")
            self.assertEqual(second.name, "약사의 혼잣말 (2).jpg")
            self.assertEqual(first.read_bytes(), data)
            self.assertEqual(second.read_bytes(), data)

    def test_invalid_image_never_creates_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TMDBError):
                save_image(b"not an image", Path(tmp), "약사의 혼잣말")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_path_characters_cannot_escape_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = save_image(sample_image(), Path(tmp), "../../약사의_혼잣말")
            self.assertEqual(saved.parent, Path(tmp).resolve())
            self.assertNotIn("_", saved.name)

    def test_skip_existing_persists_without_history_and_deleted_files_are_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = save_image(sample_image(), directory, "원래_제목")
            self.assertEqual(SavedPosters(directory).find("원래 제목"), path)
            with self.assertRaises(AlreadySaved):
                save_image(sample_image(), directory, "원래 제목", skip_existing=True)
            self.assertEqual(len(list(directory.iterdir())), 1)
            index = SavedPosters(directory)
            path.unlink()
            self.assertIsNone(index.find("원래 제목"))

    def test_previous_numbered_filename_is_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "약사의 혼잣말 (10).jpg"
            path.write_bytes(sample_image())
            (Path(tmp) / "_.jpg").write_bytes(sample_image())
            self.assertEqual(SavedPosters(Path(tmp)).find("약사의_혼잣말"), path.resolve())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = TMDBClient("private-test-key", retries=0)
        self.work = Work(1429, "tv", "약사의 혼잣말", "薬屋のひとりごと", "2023", "ja", (16,))

    def test_search_japanese_gets_korean_title_and_ignores_people(self):
        body = {"results": [
            {"id": 7, "media_type": "person", "name": "사람"},
            {"id": 8, "media_type": "tv", "name": "다른 작품", "original_name": "other"},
            {"id": 9, "media_type": "tv", "name": "약사의 혼잣말", "original_name": "薬屋のひとりごと", "first_air_date": "2023-10-22"},
        ]}
        with patch.object(self.client, "_read", return_value=json.dumps(body).encode()) as read:
            results = self.client.search("薬屋のひとりごと", "multi")
        self.assertEqual([work.id for work in results], [9, 8])
        self.assertEqual(results[0].korean_title, "약사의 혼잣말")
        self.assertEqual(results[0].year, "2023")
        params = parse_qs(urlparse(read.call_args.args[0]).query)
        self.assertEqual(params["language"], ["ko-KR"])
        self.assertEqual(params["query"], ["薬屋のひとりごと"])

    def test_only_japanese_posters_even_if_api_returns_other_languages(self):
        body = {"posters": [
            {"file_path": "/a.jpg", "iso_639_1": "en", "vote_average": 10},
            {"file_path": "/b.jpg", "iso_639_1": None},
            {"file_path": "/c.jpg", "iso_639_1": "ko"},
            {"file_path": "/d.jpg", "iso_639_1": "ja", "vote_average": 3},
            {"file_path": "/e.jpg", "iso_639_1": "ja", "vote_average": 7},
            {"file_path": "/e.jpg", "iso_639_1": "ja", "vote_average": 7},
        ]}
        with patch.object(self.client, "_json", return_value=body) as request:
            posters = self.client.posters(self.work)
        self.assertEqual([poster.file_path for poster in posters], ["/e.jpg", "/d.jpg"])
        self.assertTrue(all(poster.language == "ja" for poster in posters))
        self.assertEqual(request.call_args.kwargs["language"], "ja")
        self.assertEqual(request.call_args.kwargs["include_image_language"], "ja")

    def test_api_key_is_not_exposed_in_authentication_errors(self):
        error = HTTPError("https://example.test/?api_key=private-test-key", 401, "denied", {}, None)
        with patch("tmdb_poster.client.urlopen", side_effect=error):
            with self.assertRaises(TMDBError) as captured:
                self.client.search("약사의 혼잣말")
        self.assertNotIn("private-test-key", str(captured.exception))
        self.assertIn("인증", str(captured.exception))

    def test_transient_network_error_retries(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b'{"results": []}'
        client = TMDBClient("private-test-key", retries=1)
        with patch("tmdb_poster.client.urlopen", side_effect=[URLError("timeout"), Response()]) as request, patch("tmdb_poster.client.time.sleep"):
            self.assertEqual(client.search("약사의 혼잣말"), [])
        self.assertEqual(request.call_count, 2)

    def test_download_uses_original_size(self):
        poster = Poster("/test.jpg", "ja", 300, 450)
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.client, "image_bytes", return_value=sample_image()) as fetch:
            saved = download_poster(self.client, self.work, poster, Path(tmp), "원래_한글_제목")
        fetch.assert_called_once_with(poster, original=True)
        self.assertEqual(saved.name, "원래 한글 제목.jpg")

    def test_original_url_uses_configuration(self):
        poster = Poster("/test.jpg", "ja", 300, 450)
        config = {"images": {"secure_base_url": "https://image.tmdb.org/t/p/", "poster_sizes": ["w185", "original"]}}
        with patch.object(self.client, "_json", return_value=config):
            self.assertEqual(self.client.image_url(poster, original=True), "https://image.tmdb.org/t/p/original/test.jpg")
            self.assertEqual(self.client.image_url(poster), "https://image.tmdb.org/t/p/w185/test.jpg")

    def test_duplicate_does_not_request_another_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            save_image(sample_image(), Path(tmp), "원래 제목")
            with patch.object(self.client, "image_bytes") as fetch, self.assertRaises(AlreadySaved):
                download_poster(self.client, self.work, Poster("/a.jpg", "ja", 300, 450), Path(tmp), "원래_제목")
            fetch.assert_not_called()

    def test_anime_search_fallback_keeps_only_season_out_of_query(self):
        with patch.object(self.client, "search", side_effect=[[], [self.work]]) as search:
            result = self.client.search_anime("약사의_혼잣말 2기")
        self.assertEqual(result, [self.work])
        self.assertEqual(search.call_args.args, ("약사의 혼잣말", "multi"))

    def test_anime_search_never_shows_live_action_with_same_name(self):
        live_action = Work(1, "movie", "공각기동대: 고스트 인 더 쉘", "Ghost in the Shell", "2017", "en", (28, 878))
        with patch.object(self.client, "search", return_value=[live_action]):
            self.assertEqual(self.client.search_anime("공각기동대 더 고스트 인 더 쉘"), [])


if __name__ == "__main__":
    unittest.main()
