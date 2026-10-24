from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_core import sample_image
from test_workbook import write_workbook
from tmdb_poster.client import Poster, Work
from tmdb_poster.gui import PosterApp
from tmdb_poster.storage import save_image


class FakeClient:
    downloads = []

    def __init__(self, key, **kwargs):
        self.data = sample_image()

    def search(self, query, kind):
        return [Work(1429, "tv", "약사의 혼잣말", "薬屋のひとりごと", "2023")]

    def search_anime(self, query, kind="multi"):
        return self.search(query, kind)

    def posters(self, work):
        return [Poster(f"/{i}.jpg", "ja", 300, 450) for i in range(10)]

    def configure_images(self):
        pass

    def image_bytes(self, poster, *, original=False):
        if original:
            self.downloads.append(poster.file_path)
        return self.data


class GUIFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            patch("tmdb_poster.gui.ROOT", Path(self.tmp.name)),
            patch("tmdb_poster.gui.load_api_key", return_value="test-key"),
            patch("tmdb_poster.gui.TMDBClient", FakeClient),
            patch("tmdb_poster.gui.messagebox.showerror"),
        ]
        for item in self.patches:
            item.start()
        FakeClient.downloads = []
        self.app = PosterApp(auto_load=False)
        self.app.attributes("-alpha", 0)
        self.app.update()
        self.app.withdraw()
        self.app.update_idletasks()

    def tearDown(self):
        self.app.downloading = False
        self.app._close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def wait_for(self, condition):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.update()
            if condition():
                return
            time.sleep(0.01)
        self.fail(f"UI 작업 대기 시간 초과: {self.app.status_var.get()}")

    def load_posters(self):
        self.app.search()
        self.wait_for(lambda: not self.app.busy and len(self.app.posters) == 9)

    def test_first_nine_and_keypad_download_preserves_manual_korean_title(self):
        self.app.title_var.set("원래_한글_제목")
        self.load_posters()
        self.assertEqual(len(self.app.all_posters), 10)
        self.assertEqual(len(self.app.posters), 9)
        self.assertEqual(self.app.title_var.get(), "원래_한글_제목")
        self.app._on_key(SimpleNamespace(state=0, keysym="KP_9", char="9"))
        self.wait_for(lambda: self.app.last_saved is not None)
        self.assertEqual(self.app.last_saved.name, "원래 한글 제목.jpg")
        self.assertEqual(FakeClient.downloads, ["/8.jpg"])
        self.assertEqual(self.app.last_saved.read_bytes(), sample_image())
        history = json.loads((self.app.last_saved.parent / "download_history.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(history["original_korean_title"], "원래_한글_제목")
        self.assertEqual(history["poster"]["language"], "ja")

    def test_typing_numbers_in_filename_does_not_download(self):
        self.load_posters()
        with patch.object(self.app, "focus_get", return_value=self.app.title_entry):
            self.app._on_key(SimpleNamespace(state=0, keysym="1", char="1"))
        self.assertFalse(self.app.downloading)
        self.assertEqual(FakeClient.downloads, [])

    def test_korean_title_populates_and_tenth_poster_cannot_be_saved(self):
        self.app.query_var.set("薬屋のひとりごと")
        self.load_posters()
        self.assertEqual(self.app.title_var.get(), "약사의 혼잣말")
        self.app.select_poster(9)
        self.assertFalse(self.app.downloading)
        self.assertEqual(FakeClient.downloads, [])

    def test_default_window_has_room_for_all_nine_cards_and_sidebar(self):
        self.assertEqual(len(self.app.cards), 9)
        for frame, picture, info, caption in self.app.cards:
            self.assertGreater(frame.winfo_width(), 120)
            self.assertGreater(frame.winfo_height(), 110)
            self.assertLessEqual(frame.winfo_y() + frame.winfo_height(), self.app.card_grid.winfo_height())
        sidebar = self.app.title_list.master.master
        footer = sidebar.winfo_children()[-1]
        self.assertGreaterEqual(footer.winfo_height(), footer.winfo_reqheight())

    def test_excel_save_advance_skip_existing_and_go_back(self):
        path = Path(self.tmp.name) / "제목 목록.xlsx"
        write_workbook(path, ["첫_번째_제목", "두 번째 제목", "세 번째 제목"])
        save_image(sample_image(), Path(self.app.directory_var.get()), "두 번째 제목")
        self.app.load_excel(path)
        self.wait_for(lambda: not self.app.busy and len(self.app.posters) == 9)
        self.assertEqual(self.app.item_index, 0)
        self.app._on_key(SimpleNamespace(state=0, keysym="KP_9", char="9"))
        self.wait_for(lambda: self.app.item_index == 2 and not self.app.busy)
        self.assertEqual(self.app.last_saved.name, "첫 번째 제목.jpg")
        self.assertEqual(self.app.title_var.get(), "세 번째 제목")
        history = json.loads((self.app.last_saved.parent / "download_history.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(history["source"]["row"], 2)
        self.assertEqual(history["source"]["sheet"], "목록")
        # 같은 키를 계속 누르고 있어도 다음 작품이 자동으로 저장되면 안 됩니다.
        self.app._on_key(SimpleNamespace(state=0, keysym="KP_9", char="9"))
        self.assertEqual(FakeClient.downloads, ["/8.jpg"])
        self.app.go_previous()
        self.wait_for(lambda: self.app.item_index == 1 and not self.app.busy)
        self.app.select_poster(0)
        self.assertEqual(FakeClient.downloads, ["/8.jpg"])
        self.assertIn("이미 저장", self.app.status_var.get())

    def test_excel_reload_resumes_at_an_unsaved_title(self):
        path = Path(self.tmp.name) / "목록.xlsx"
        write_workbook(path, ["저장된_제목", "남은 제목"])
        save_image(sample_image(), Path(self.app.directory_var.get()), "저장된 제목")
        self.app.load_excel(path)
        self.wait_for(lambda: self.app.item_index == 1 and not self.app.busy)
        self.assertEqual(self.app.title_var.get(), "남은 제목")
        self.assertTrue(self.app.skip_existing_var.get())
        self.assertIn("저장 1", self.app.queue_var.get())

    def test_duplicate_prevention_can_be_explicitly_disabled(self):
        self.load_posters()
        save_image(sample_image(), Path(self.app.directory_var.get()), "약사의 혼잣말")
        self.app.skip_existing_var.set(False)
        self.app._skip_changed()
        self.app.select_poster(0)
        self.wait_for(lambda: self.app.last_saved is not None)
        self.assertEqual(self.app.last_saved.name, "약사의 혼잣말 (2).jpg")

    def _enable_tk_keyboard_events(self, widget=None):
        # 직접 _on_key를 호출하지 않고 Tk 이벤트 큐와 위젯 바인딩을 통과시킵니다.
        self.app.deiconify()
        (widget or self.app).focus_force()
        self.app.update()

    def _tk_key(self, widget=None, **options):
        widget = widget or self.app
        widget.event_generate("<KeyPress>", **options)
        widget.event_generate("<KeyRelease>", **options)
        self.app.update()

    @unittest.skipUnless(sys.platform == "win32", "Windows Tk의 실제 키패드 매핑 검증")
    def test_numlock_on_tk_keypad_downloads_ninth_poster(self):
        self.load_posters()
        self._enable_tk_keyboard_events()
        self._tk_key(keycode=0x69, state=0x0008)  # VK_NUMPAD9, Num Lock ON
        self.wait_for(lambda: self.app.last_saved is not None)
        self.assertEqual(FakeClient.downloads, ["/8.jpg"])
        self.assertFalse(self.app.pressed_keys)

    @unittest.skipUnless(sys.platform == "win32", "Windows Tk Num Lock 상태 검증")
    def test_numlock_on_tk_arrows_go_back_and_skip_without_saving(self):
        path = Path(self.tmp.name) / "키보드 목록.xlsx"
        write_workbook(path, ["첫 번째", "두 번째", "세 번째"])
        self.app.load_excel(path)
        self.wait_for(lambda: not self.app.busy and bool(self.app.posters))
        self._enable_tk_keyboard_events(self.app.title_list)
        self._tk_key(self.app.title_list, keysym="Right", state=0x40008)
        self.wait_for(lambda: self.app.item_index == 1 and not self.app.busy)
        self._tk_key(keysym="Left", state=0x40008)
        self.wait_for(lambda: self.app.item_index == 0 and not self.app.busy)
        self.assertEqual(FakeClient.downloads, [])

    @unittest.skipUnless(sys.platform == "win32", "Windows Tk 조합키 검증")
    def test_tk_control_alt_combinations_still_do_not_download(self):
        self.load_posters()
        self._enable_tk_keyboard_events()
        for modifier in (0x0004, 0x0010, 0x20000):
            self._tk_key(keycode=0x61, state=0x0008 | modifier)
        self.assertEqual(FakeClient.downloads, [])
        self.assertFalse(self.app.downloading)

    @unittest.skipUnless(sys.platform == "win32", "Windows Tk 읽기 전용 입력란 키패드 검증")
    def test_tk_keypad_works_from_readonly_filename_entry(self):
        self.load_posters()
        self.app.title_entry.configure(state="readonly")
        self._enable_tk_keyboard_events(self.app.title_entry)
        self._tk_key(self.app.title_entry, keycode=0x61, state=0x0008)
        self.wait_for(lambda: self.app.last_saved is not None)
        self.assertEqual(FakeClient.downloads, ["/0.jpg"])

    def test_bulk_button_processes_entire_workbook_and_always_skips_saved(self):
        path = Path(self.tmp.name) / "전체.xlsx"
        write_workbook(path, ["이미 저장", "이전_미저장", "현재 제목"])
        save_image(sample_image(), Path(self.app.directory_var.get()), "이미 저장")
        self.app.load_excel(path)
        self.wait_for(lambda: not self.app.busy and bool(self.app.posters))
        self.app._display_item(2)
        self.wait_for(lambda: not self.app.busy)
        self.app.skip_existing_var.set(False)
        self.app.bulk_button.invoke()
        self.assertTrue(self.app.bulk_running)
        self.assertEqual(str(self.app.next_button.cget("state")), "disabled")
        self.wait_for(lambda: not self.app.bulk_running)
        self.assertEqual((self.app.bulk_summary.saved, self.app.bulk_summary.skipped), (2, 1))
        self.assertTrue((Path(self.app.directory_var.get()) / "이전 미저장.jpg").is_file())
        self.assertEqual(FakeClient.downloads, ["/0.jpg", "/0.jpg"])
        self.app.bulk_button.invoke()
        self.wait_for(lambda: not self.app.bulk_running)
        self.assertEqual((self.app.bulk_summary.saved, self.app.bulk_summary.skipped), (0, 3))
        self.assertEqual(FakeClient.downloads, ["/0.jpg", "/0.jpg"])

    def test_bulk_stop_button_stops_before_saving_an_inflight_image(self):
        path = Path(self.tmp.name) / "중지.xlsx"
        write_workbook(path, ["첫 제목", "둘째 제목"])
        self.app.load_excel(path)
        self.wait_for(lambda: not self.app.busy and bool(self.app.posters))
        started, release = threading.Event(), threading.Event()

        class SlowClient(FakeClient):
            def image_bytes(self, poster, *, original=False):
                if original:
                    started.set()
                    release.wait(4)
                return super().image_bytes(poster, original=original)

        try:
            with patch("tmdb_poster.gui.TMDBClient", SlowClient):
                self.app.bulk_button.invoke()
            self.wait_for(started.is_set)
            self.app.bulk_stop_button.invoke()
            self.assertTrue(self.app.bulk_stop.is_set())
        finally:
            release.set()
        self.wait_for(lambda: not self.app.bulk_running)
        self.assertTrue(self.app.bulk_summary.stopped)
        self.assertEqual(self.app.bulk_summary.saved, 0)
        self.assertFalse(list(Path(self.app.directory_var.get()).glob("*.jpg")))
        self.assertEqual(str(self.app.bulk_button.cget("state")), "normal")


if __name__ == "__main__":
    unittest.main()
