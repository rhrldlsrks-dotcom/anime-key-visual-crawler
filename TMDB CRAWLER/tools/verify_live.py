"""실제 API로 숨긴 GUI의 엑셀 검색, 9개 미리보기, 키패드 저장 경로를 확인합니다."""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from tmdb_poster.gui import PosterApp
from tmdb_poster.storage import SavedPosters


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true", help="9번 키패드 입력으로 실제 원본 1장 저장")
    parser.add_argument("--excel", type=Path, help="검증할 엑셀 파일")
    parser.add_argument("--row", type=int, help="검증할 A열의 실제 행 번호")
    args = parser.parse_args()
    app = PosterApp(auto_load=False)
    app.skip_existing_var.set(True)
    app.attributes("-alpha", 0)
    app.update()
    app.withdraw()
    errors = []

    def wait_for(condition):
        deadline = time.monotonic() + 65
        while time.monotonic() < deadline:
            app.update()
            if errors:
                raise RuntimeError(errors[0])
            if condition():
                return
            time.sleep(0.02)
        raise RuntimeError(f"확인 시간 초과: {app.status_var.get()}")

    saved_settings = patch.object(app, "_save_settings")
    saved_settings.start()
    try:
        with patch("tmdb_poster.gui.messagebox.showerror", side_effect=lambda title, text, **kwargs: errors.append(text)):
            if args.excel:
                before = hashlib.sha256(args.excel.read_bytes()).hexdigest()
                app.load_excel(args.excel)
                assert hashlib.sha256(args.excel.read_bytes()).hexdigest() == before
                if args.row:
                    index = next(i for i, item in enumerate(app.workbook.items) if item.row == args.row)
                    app._display_item(index)
                print("엑셀 제목 수:", len(app.workbook.items))
            else:
                app.search()
            wait_for(lambda: not app.busy and bool(app.posters))
            assert len(app.posters) <= 9
            assert all(poster.language == "ja" for poster in app.posters)
            wait_for(lambda: all(photo is not None for photo in app.photos[:len(app.posters)]))
            print("작품:", app.work.label)
            print("등록된 일본어 포스터:", len(app.all_posters))
            print("표시한 일본어 미리보기:", len(app.posters))
            if args.download:
                app.directory_var.set(str(Path(__file__).resolve().parents[1] / "downloads"))
                app.saved = SavedPosters(Path(app.directory_var.get()))
                if not app.workbook:
                    app.title_var.set("약사의_혼잣말")
                title = app.workbook.items[app.item_index].title if app.workbook else app.title_var.get()
                if app.saved.find(title):
                    app.select_poster(0)
                    assert not app.downloading and app.last_saved is None
                    print("중복 저장 차단: 정상")
                    return
                # 키패드 이벤트 처리부터 다운로드 완료 이벤트까지 실제 UI 경로를 확인합니다.
                slot = min(9, len(app.posters))
                expected_size = (app.posters[slot - 1].width, app.posters[slot - 1].height)
                app._on_key(SimpleNamespace(state=0, keysym=f"KP_{slot}", char=str(slot)))
                wait_for(lambda: app.last_saved is not None)
                assert "_" not in app.last_saved.name
                with Image.open(app.last_saved) as image:
                    assert image.size == expected_size
                    print("저장:", app.last_saved)
                    print("원본 해상도:", image.size)
                    print("파일 크기:", app.last_saved.stat().st_size, "bytes")
    finally:
        app.downloading = False
        app._close()
        saved_settings.stop()


if __name__ == "__main__":
    main()
