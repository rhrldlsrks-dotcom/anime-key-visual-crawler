from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="TMDB 포스터 다운로더")
    parser.add_argument("--check", action="store_true", help="프로그램 창을 열지 않고 실행 환경 확인")
    args = parser.parse_args()
    try:
        from tmdb_poster.gui import PosterApp
    except ImportError as error:
        if error.name and error.name.startswith("PIL"):
            print("이미지 미리보기에 Pillow가 필요합니다. 실행.cmd를 다시 실행해 주세요.")
            sys.exit(1)
        raise
    if args.check:
        print(f"Python {sys.version.split()[0]}: OK")
        print("Tkinter and Pillow: OK")
        print("Startup check passed.")
        return
    app = PosterApp()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        log_path = Path(__file__).resolve().parent / "error.log"
        log_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise
