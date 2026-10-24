from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from .bulk import BulkSummary, download_first_posters
from .client import TMDBClient, TMDBError, Work
from .config import DEFAULT_EXCEL, DEFAULT_KOREAN_TITLE, DEFAULT_QUERY, ROOT, load_api_key, save_api_key
from .storage import AlreadySaved, SavedPosters, download_poster, history_record, safe_title
from .workbook import WorkbookTitles, read_titles

BG = "#101827"
PANEL = "#192438"
CARD = "#202e44"
TEXT = "#edf3fb"
MUTED = "#a5b4cb"
ACCENT = "#70dfc0"
PAGE_SIZE = 9


class PosterApp(tk.Tk):
    def __init__(self, *, auto_load: bool = True):
        super().__init__()
        window_system = str(self.tk.call("tk", "windowingsystem"))
        # Windows Tk의 Mod1(0x8)은 Num Lock입니다. Alt는 Mod2/AltMask입니다.
        self.shortcut_modifier_mask = {"win32": 0x20014, "aqua": 0x001C}.get(window_system, 0x000C)
        self.title("TMDB 애니 포스터 · 엑셀 A열")
        self.configure(bg=BG)
        self.geometry(f"{min(1360, self.winfo_screenwidth() - 70)}x{min(950, self.winfo_screenheight() - 100)}")
        self.minsize(1000, 690)
        self.settings = self._load_settings()
        self.events = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=9, thread_name_prefix="poster-thumb")
        self.client = None
        self.works = []
        self.work = None
        self.all_posters = []
        self.posters = []
        self.workbook: WorkbookTitles | None = None
        self.item_index = -1
        self.generation = 0
        self.busy = False
        self.downloading = False
        self.closed = False
        self.bulk_running = False
        self.bulk_stop = threading.Event()
        self.bulk_summary = None
        self.close_after_bulk = False
        self.cache = OrderedDict()
        self.search_cache = {}
        self.poster_cache = {}
        self.photos = []
        self.cards = []
        self.thumbnail_futures = []
        self.last_saved = None
        self.api_key = load_api_key()
        self.manual_title = False
        self.setting_title = True
        self.pressed_keys = set()
        self.advance_timer = None
        self.restore_timer = None
        self._build_styles()
        self._build_ui()
        self.setting_title = False
        self.saved = SavedPosters(Path(self.directory_var.get()))
        self._update_batch_controls()
        self.bind_all("<KeyPress>", self._on_key)
        self.bind_all("<KeyRelease>", lambda event: self.pressed_keys.discard(event.keysym))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.event_timer = self.after(65, self._drain_events)
        if auto_load:
            self.restore_timer = self.after(200, self._restore_workbook)

    def _load_settings(self):
        try:
            value = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _build_styles(self):
        self.option_add("*Font", ("맑은 고딕", 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, foreground=TEXT, arrowcolor=TEXT)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", TEXT)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=BG,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)

    def _label(self, parent, text="", **kwargs):
        return tk.Label(parent, text=text, bg=kwargs.pop("bg", PANEL), fg=kwargs.pop("fg", TEXT), **kwargs)

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        return tk.Button(parent, text=text, command=command, bg=ACCENT if primary else CARD,
                         fg=BG if primary else TEXT, activebackground="#8cebd2" if primary else "#30445f",
                         activeforeground=BG if primary else TEXT, relief="flat", borderwidth=0,
                         padx=10, pady=6, cursor="hand2", disabledforeground="#66748a", **kwargs)

    def _entry(self, parent, variable, **kwargs):
        return tk.Entry(parent, textvariable=variable, bg=CARD, readonlybackground=CARD, fg=TEXT,
                        insertbackground=TEXT, relief="flat", highlightthickness=1,
                        highlightbackground="#34445b", highlightcolor=ACCENT, **kwargs)

    def _build_ui(self):
        toolbar = tk.Frame(self, bg=PANEL, padx=14, pady=9)
        toolbar.pack(fill="x")
        self.excel_button = self._button(toolbar, "엑셀 A열 불러오기", self.choose_excel, primary=True)
        self.excel_button.pack(side="left")
        self.workbook_var = tk.StringVar(value="엑셀 파일을 선택하거나 개별 검색을 사용하세요")
        self._label(toolbar, textvariable=self.workbook_var, anchor="w").pack(side="left", fill="x", expand=True, padx=12)
        self.manual_button = self._button(toolbar, "개별 검색", self.manual_mode)
        self.manual_button.pack(side="right", padx=(8, 0))
        self.api_button = self._button(toolbar, "API 키 설정", self.edit_key)
        self.api_button.pack(side="right")

        bulkbar = tk.Frame(self, bg=BG, padx=14, pady=5)
        bulkbar.pack(fill="x")
        self.bulk_button = self._button(bulkbar, "미저장 전체 · 첫 포스터 저장", self.start_bulk, primary=True)
        self.bulk_button.pack(side="left")
        self.bulk_stop_button = self._button(bulkbar, "자동 저장 중지", self.stop_bulk)
        self.bulk_stop_button.pack(side="left", padx=8)
        self.bulk_progress_var = tk.StringVar(value="엑셀 전체 대상 · 이미 저장한 제목은 항상 제외")
        self._label(bulkbar, textvariable=self.bulk_progress_var, bg=BG, fg=MUTED, anchor="w").pack(side="left", fill="x", expand=True)

        sidebar = tk.Frame(self, bg=PANEL, width=310, padx=14, pady=10)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self.queue_var = tk.StringVar(value="제목 목록")
        self._label(sidebar, textvariable=self.queue_var, fg=ACCENT, anchor="w").pack(fill="x", pady=(0, 5))
        navigation = tk.Frame(sidebar, bg=PANEL)
        navigation.pack(fill="x", pady=(0, 6))
        self.previous_button = self._button(navigation, "← 되돌아가기", self.go_previous)
        self.previous_button.pack(side="left", fill="x", expand=True, padx=(0, 3))
        self.next_button = self._button(navigation, "다음 / 건너뛰기 →", self.go_next)
        self.next_button.pack(side="left", fill="x", expand=True, padx=(3, 0))
        list_frame = tk.Frame(sidebar, bg=PANEL)
        list_frame.pack(fill="both", expand=True, pady=(0, 9))
        self.title_list = tk.Listbox(list_frame, bg=BG, fg=TEXT, selectbackground="#31516a",
                                    selectforeground=TEXT, relief="flat", borderwidth=0,
                                    highlightthickness=0, exportselection=False, height=4)
        self.title_list.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.title_list.yview)
        scrollbar.pack(side="right", fill="y")
        self.title_list.configure(yscrollcommand=scrollbar.set)
        self.title_list.bind("<<ListboxSelect>>", self._select_item)

        self._label(sidebar, "검색어 · 결과가 없으면 직접 수정", fg=MUTED, anchor="w").pack(fill="x")
        self.query_var = tk.StringVar(value=DEFAULT_QUERY)
        self.query_entry = self._entry(sidebar, self.query_var)
        self.query_entry.pack(fill="x", ipady=6, pady=(4, 5))
        self.query_entry.bind("<Return>", lambda event: self.search())
        search_row = tk.Frame(sidebar, bg=PANEL)
        search_row.pack(fill="x", pady=(0, 8))
        self.kind_var = tk.StringVar(value="TV + 영화")
        self.kind_combo = ttk.Combobox(search_row, textvariable=self.kind_var, state="readonly",
                                       values=("TV + 영화", "TV / 애니메이션", "영화 / 극장판"), width=17)
        self.kind_combo.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 6))
        self.search_button = self._button(search_row, "검색 ↵", self.search, primary=True)
        self.search_button.pack(side="right")
        self._label(sidebar, "원래 한글 제목 · 저장명", fg=MUTED, anchor="w").pack(fill="x")
        self.title_var = tk.StringVar(value=DEFAULT_KOREAN_TITLE)
        self.title_entry = self._entry(sidebar, self.title_var)
        self.title_entry.pack(fill="x", ipady=6, pady=(4, 3))
        self.title_hint = self._label(sidebar, "", fg=ACCENT, anchor="w", font=("맑은 고딕", 9))
        self.title_hint.pack(fill="x", pady=(0, 7))
        self.title_var.trace_add("write", self._update_filename)
        self._update_filename()

        self._label(sidebar, "저장 폴더", fg=MUTED, anchor="w").pack(fill="x")
        self.directory_var = tk.StringVar(value=str(self.settings.get("directory") or ROOT / "downloads"))
        self.directory_entry = self._entry(sidebar, self.directory_var, state="readonly")
        self.directory_entry.pack(fill="x", ipady=4, pady=(3, 4))
        folder_row = tk.Frame(sidebar, bg=PANEL)
        folder_row.pack(fill="x", pady=(0, 5))
        self.folder_change_button = self._button(folder_row, "폴더 변경", self.choose_directory)
        self.folder_change_button.pack(side="left")
        self._button(folder_row, "열기", self.open_directory).pack(side="right")
        self.skip_existing_var = tk.BooleanVar(value=self.settings.get("skip_existing", True))
        self.skip_checkbox = tk.Checkbutton(sidebar, text="이미 저장한 제목 건너뛰기", variable=self.skip_existing_var,
                                           command=self._skip_changed, bg=PANEL, fg=TEXT, selectcolor=CARD,
                                           activebackground=PANEL, activeforeground=TEXT, anchor="w")
        self.skip_checkbox.pack(fill="x", pady=(0, 4))
        self._label(sidebar, "숫자 1–9: 저장 후 다음 제목\n← 되돌아가기 · → 건너뛰기\nNum Lock 켜기 · 이미지 클릭도 가능", fg=MUTED,
                    font=("맑은 고딕", 9), justify="left", anchor="w").pack(fill="x", pady=(0, 5))
        self._label(sidebar, "Uses the TMDB API; not endorsed\nor certified by TMDB.", fg=MUTED,
                    font=("맑은 고딕", 7), justify="left", anchor="w").pack(side="bottom", fill="x")

        content = tk.Frame(self, bg=BG, padx=14, pady=12)
        content.pack(side="left", fill="both", expand=True)
        self.current_title_var = tk.StringVar(value="포스터를 보고, 1–9번으로 저장하세요")
        self._label(content, textvariable=self.current_title_var, bg=BG, font=("맑은 고딕", 16, "bold"),
                    anchor="w").pack(fill="x")
        self._label(content, "일본어 포스터 최대 9장 · A열 한글 제목으로 원본 저장 · _는 공백으로",
                    bg=BG, fg=MUTED, anchor="w", font=("맑은 고딕", 9)).pack(fill="x", pady=(2, 8))
        self.result_var = tk.StringVar(value="엑셀을 불러오거나 왼쪽에서 검색하세요")
        self.result_combo = ttk.Combobox(content, textvariable=self.result_var, state="disabled")
        self.result_combo.pack(fill="x", ipady=3)
        self.result_combo.bind("<<ComboboxSelected>>", self._select_work)
        self.count_var = tk.StringVar(value="일본어 포스터")
        self._label(content, textvariable=self.count_var, bg=BG, fg=MUTED, anchor="w").pack(fill="x", pady=(5, 3))
        self.card_grid = tk.Frame(content, bg=BG)
        self.card_grid.pack(fill="both", expand=True)
        for column in range(3):
            self.card_grid.columnconfigure(column, weight=1, uniform="poster")
        for row in range(3):
            self.card_grid.rowconfigure(row, weight=1, uniform="poster")
        for index in range(PAGE_SIZE):
            frame = tk.Frame(self.card_grid, bg=CARD, padx=5, pady=4, cursor="hand2")
            frame.grid(row=index // 3, column=index % 3, padx=3, pady=3, sticky="nsew")
            top = tk.Frame(frame, bg=CARD)
            top.pack(fill="x")
            number = self._label(top, str(index + 1), bg=ACCENT, fg=BG, font=("맑은 고딕", 9, "bold"), width=3)
            number.pack(side="left")
            info = self._label(top, "", bg=CARD, fg=MUTED, font=("맑은 고딕", 8))
            info.pack(side="right")
            picture = self._label(frame, "検索 / 검색 대기", bg=CARD, fg=MUTED, cursor="hand2")
            picture.pack(fill="both", expand=True, pady=(3, 1))
            caption = self._label(frame, "", bg=CARD, fg=MUTED, font=("맑은 고딕", 8))
            caption.pack(fill="x")
            for widget in (frame, top, number, info, picture, caption):
                widget.bind("<Button-1>", lambda event, slot=index: self.select_poster(slot))
            self.cards.append((frame, picture, info, caption))
        self.progress = ttk.Progressbar(content, mode="indeterminate")
        self.progress.pack(fill="x", pady=(5, 4))
        self.status_var = tk.StringVar(value="엑셀 A열을 불러오면 제목별 포스터를 순서대로 표시합니다.")
        self._label(content, textvariable=self.status_var, bg=BG, fg=ACCENT, anchor="w", justify="left",
                    wraplength=850, height=2, font=("맑은 고딕", 9)).pack(fill="x")
        self.last_saved_var = tk.StringVar(value="")
        self._label(content, textvariable=self.last_saved_var, bg=BG, fg=MUTED, anchor="w",
                    font=("맑은 고딕", 8)).pack(fill="x")

    def _update_filename(self, *_):
        if not self.setting_title:
            self.manual_title = True
        try:
            name = safe_title(self.title_var.get())
            self.title_hint.configure(text=f"저장명: {name[:29]}{'…' if len(name) > 29 else ''}")
        except ValueError:
            self.title_hint.configure(text="저장할 한글 제목을 입력해 주세요.")

    def _run(self, function, event_name, generation):
        def worker():
            try:
                self.events.put((event_name, generation, function()))
            except Exception as error:
                self.events.put(("error", generation, self._error_message(error)))
        threading.Thread(target=worker, daemon=True, name=event_name).start()

    @staticmethod
    def _error_message(error):
        if isinstance(error, (TMDBError, ValueError)):
            return str(error)
        if isinstance(error, PermissionError):
            return "이 폴더에 접근할 권한이 없습니다. 다른 폴더를 선택해 주세요."
        if isinstance(error, OSError):
            return "파일 작업에 실패했습니다. 저장 경로와 디스크 공간을 확인해 주세요."
        return "작업을 완료하지 못했습니다. 다시 검색하거나 다음 제목으로 넘어가 주세요."

    def _cancel_pending(self):
        self.generation += 1
        self._cancel_thumbnails()
        if self.advance_timer is not None:
            self.after_cancel(self.advance_timer)
            self.advance_timer = None
        self._set_busy(False)

    def _cancel_thumbnails(self):
        for future in self.thumbnail_futures:
            future.cancel()
        self.thumbnail_futures.clear()

    def _set_busy(self, value, message=None):
        self.busy = value
        disabled = value or self.downloading or self.bulk_running
        self.search_button.configure(state="disabled" if disabled else "normal")
        self.query_entry.configure(state="disabled" if disabled else "normal")
        self.kind_combo.configure(state="disabled" if disabled else "readonly")
        self.result_combo.configure(state="disabled" if disabled or not self.works else "readonly")
        locked = self.downloading or self.bulk_running
        self.title_entry.configure(state="disabled" if locked else "readonly" if self.workbook else "normal")
        self.excel_button.configure(state="disabled" if locked else "normal")
        self.manual_button.configure(state="disabled" if locked else "normal")
        self.api_button.configure(state="disabled" if disabled else "normal")
        self.folder_change_button.configure(state="disabled" if locked else "normal")
        self.skip_checkbox.configure(state="disabled" if locked else "normal")
        if self.bulk_running:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(1, self.bulk_summary.total), value=self.bulk_summary.processed)
        elif disabled:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="indeterminate", value=0)
        if message:
            self.status_var.set(message)
        self._update_batch_controls()

    def search(self):
        if self.busy or self.downloading or self.bulk_running:
            return
        query = self.query_var.get().strip()
        if not query:
            self.status_var.set("검색할 작품 제목을 입력해 주세요.")
            return
        if not self.api_key:
            self.edit_key()
            return
        self._cancel_pending()
        if self.client is None:
            self.client = TMDBClient(self.api_key)
        client = self.client
        kind = {"TV + 영화": "multi", "TV / 애니메이션": "tv", "영화 / 극장판": "movie"}[self.kind_var.get()]
        self.works, self.work, self.all_posters, self.posters = [], None, [], []
        self.result_combo.configure(values=())
        self.result_var.set("작품 검색 중…")
        self._render_posters()
        self._set_busy(True, f"‘{query}’ 검색 중…")
        key = (query, kind)

        def find():
            if key not in self.search_cache:
                self.search_cache[key] = client.search_anime(query, kind)
            return self.search_cache[key]
        self._run(find, "search", self.generation)

    def _select_work(self, event=None):
        if not self.busy and not self.downloading and not self.bulk_running:
            index = self.result_combo.current()
            if 0 <= index < len(self.works):
                self._load_work(self.works[index])

    def _load_work(self, work):
        self.generation += 1
        self._cancel_thumbnails()
        self.work = work
        self.all_posters, self.posters = [], []
        if not self.workbook and not self.manual_title:
            self.setting_title = True
            self.title_var.set(work.korean_title if work.has_korean_title else "")
            self.setting_title = False
        self._render_posters()
        self._set_busy(True, f"{work.korean_title} · 일본어 포스터 불러오는 중…")
        client = self.client
        key = (work.media_type, work.id)

        def load():
            if key not in self.poster_cache:
                self.poster_cache[key] = client.posters(work)
            client.configure_images()
            return self.poster_cache[key]
        self._run(load, "posters", self.generation)

    def _render_posters(self):
        self.photos = [None] * PAGE_SIZE
        self.count_var.set(f"일본어 포스터 {len(self.all_posters)}장 · {len(self.posters)}장 표시")
        for slot, (frame, picture, info, caption) in enumerate(self.cards):
            picture.configure(image="", text="등록된 포스터 없음")
            frame.configure(highlightthickness=0)
            info.configure(text="")
            caption.configure(text="")
            if slot >= len(self.posters):
                continue
            poster = self.posters[slot]
            info.configure(text="日本語")
            caption.configure(text=f"{poster.width:,} × {poster.height:,}")
            picture.configure(text="미리보기 로딩…")
            if poster.file_path in self.cache:
                self.cache.move_to_end(poster.file_path)
                self._show_thumbnail(slot, self.cache[poster.file_path])
            elif self.client:
                self.thumbnail_futures.append(self.executor.submit(self._fetch_thumbnail, self.client, poster, slot, self.generation))

    def _fetch_thumbnail(self, client, poster, slot, generation):
        try:
            self.events.put(("thumbnail", generation, (slot, poster.file_path, client.image_bytes(poster))))
        except Exception:
            self.events.put(("thumbnail_error", generation, slot))

    def _show_thumbnail(self, slot, data):
        try:
            with Image.open(BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((max(70, min(230, self.card_grid.winfo_width() // 3 - 22)),
                                 max(45, min(260, self.card_grid.winfo_height() // 3 - 60))),
                                Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image)
            self.photos[slot] = photo
            self.cards[slot][1].configure(image=photo, text="")
        except (OSError, ValueError, Image.DecompressionBombError):
            self.cards[slot][1].configure(image="", text="미리보기 표시 실패\n번호로 원본 저장 가능")

    def _on_key(self, event):
        if self.bulk_running:
            return
        if event.state & self.shortcut_modifier_mask:
            return
        focus = self.focus_get()
        if isinstance(focus, ttk.Combobox) and str(focus.cget("state")) == "readonly":
            return
        if isinstance(focus, (tk.Entry, ttk.Entry, tk.Text)) and str(focus.cget("state")) == "normal":
            return
        keypad = {"KP_End": 0, "KP_Down": 1, "KP_Next": 2, "KP_Left": 3, "KP_Begin": 4,
                  "KP_Right": 5, "KP_Home": 6, "KP_Up": 7, "KP_Prior": 8}
        slot = keypad.get(event.keysym)
        if event.char and event.char in "123456789":
            slot = int(event.char) - 1
        elif event.keysym.startswith("KP_") and event.keysym[3:] in tuple("123456789"):
            slot = int(event.keysym[3:]) - 1
        if slot is not None:
            if event.keysym not in self.pressed_keys:
                self.pressed_keys.add(event.keysym)
                self.select_poster(slot)
            return "break"
        if event.keysym in ("Left", "BackSpace"):
            self.go_previous()
            return "break"
        if event.keysym == "Right":
            self.go_next()
            return "break"

    def start_bulk(self):
        if self.bulk_running or self.downloading or not self.workbook:
            return
        if not self.api_key:
            self.edit_key()
            return
        self._cancel_pending()
        self.bulk_stop = threading.Event()
        self.bulk_summary = BulkSummary(total=len(self.workbook.items))
        self.bulk_running = True
        self.close_after_bulk = False
        self.works, self.work, self.all_posters, self.posters = [], None, [], []
        self.result_combo.configure(values=())
        self.result_var.set("자동 저장: 각 제목의 첫 번째 애니메이션 후보 / 첫 일본어 포스터")
        self._render_posters()
        self.count_var.set("자동 저장 중에는 첫 포스터 원본만 내려받습니다.")
        self.bulk_progress_var.set(f"0 / {self.bulk_summary.total} · 미저장 제목의 첫 포스터 자동 저장")
        self._set_busy(True, "엑셀 전체를 확인하고 미저장 제목의 첫 포스터를 저장합니다.")
        workbook, directory, stop = self.workbook, Path(self.directory_var.get()), self.bulk_stop
        generation = self.generation
        client = TMDBClient(self.api_key, timeout=12, retries=1)

        def update(payload):
            self.events.put(("bulk_update", generation, payload))
        self._run(lambda: download_first_posters(workbook, client, directory, stop, update), "bulk_done", generation)

    def stop_bulk(self):
        if not self.bulk_running:
            return
        self.bulk_stop.set()
        self.bulk_stop_button.configure(state="disabled")
        self.status_var.set("중지 요청됨 · 현재 서버 요청이 끝나면 멈춥니다. 저장된 파일은 유지됩니다.")

    def _show_bulk_update(self, update):
        self.bulk_summary = update.stats
        self.item_index = update.index
        self.setting_title = True
        self.title_var.set(update.item.title)
        self.setting_title = False
        self.query_var.set(update.item.title.replace("_", " "))
        self.current_title_var.set(f"A{update.item.row} · {update.item.title.replace('_', ' ')[:64]}")
        if update.path is not None:
            self.saved.add(update.path)
            if update.state == "saved":
                self.last_saved = update.path
                self.last_saved_var.set(f"최근 저장: {update.path.name}")
        self.title_list.configure(state="normal")
        if update.state in ("saved", "exists"):
            self.title_list.delete(update.index)
            self.title_list.insert(update.index, f"✓ {update.item.row:>3}  {update.item.title.replace('_', ' ')}")
            self.title_list.itemconfigure(update.index, fg=ACCENT)
        self.title_list.selection_clear(0, "end")
        self.title_list.selection_set(update.index)
        self.title_list.see(update.index)
        self.title_list.configure(state="disabled")
        stats = update.stats
        self.progress.configure(value=stats.processed)
        self.bulk_progress_var.set(f"{stats.processed} / {stats.total} · 저장 {stats.saved} · 기존 제외 {stats.skipped} · 없음 {stats.missing} · 실패 {stats.failed}")
        self._update_batch_controls()
        if not self.bulk_stop.is_set():
            labels = {"searching": "첫 포스터 검색 중", "saved": "원본 저장 완료", "exists": "이미 저장되어 제외",
                      "not_found": "작품 검색 결과 없음", "no_posters": "일본어 포스터 없음", "error": "실패"}
            self.status_var.set(f"{update.index + 1} / {stats.total} · {labels[update.state]} · {update.item.title}")

    def _finish_bulk(self, summary):
        self.bulk_summary = summary
        self.bulk_running = False
        message = (f"자동 저장 {'중지' if summary.stopped else '완료'} · 저장 {summary.saved} · 기존 제외 {summary.skipped}"
                   f" · 검색/포스터 없음 {summary.missing} · 실패 {summary.failed}")
        if summary.halt_reason:
            message += f" · {summary.halt_reason}"
        self._set_busy(False, message)
        try:
            self.saved.refresh()
            self._refresh_list()
        except OSError:
            self.status_var.set(message + " · 저장 폴더를 다시 열어 확인해 주세요.")
        self.bulk_progress_var.set(f"{summary.processed} / {summary.total} · 결과: {summary.report.name}" if summary.report else message)
        self._save_settings()
        self.focus_set()
        if self.close_after_bulk:
            self._close()

    def _restore_workbook(self):
        self.restore_timer = None
        path = Path(self.settings.get("last_excel") or DEFAULT_EXCEL)
        if path.is_file():
            self.load_excel(path)

    def choose_excel(self):
        if self.downloading or self.bulk_running:
            return
        initial = Path(self.settings.get("last_excel") or DEFAULT_EXCEL)
        filename = filedialog.askopenfilename(parent=self, title="A열에 제목이 있는 엑셀 선택",
                                              initialdir=initial.parent if initial.parent.is_dir() else ROOT,
                                              initialfile=initial.name, filetypes=[("Excel 통합 문서", "*.xlsx")])
        if filename:
            self.load_excel(Path(filename))

    def load_excel(self, path):
        if self.downloading or self.bulk_running:
            return
        try:
            workbook = read_titles(path)
            self.saved.refresh()
        except (ValueError, OSError) as error:
            messagebox.showerror("엑셀 불러오기", self._error_message(error), parent=self)
            return
        self._cancel_pending()
        same_file = str(workbook.path) == self.settings.get("last_excel")
        start = int(self.settings.get("excel_index", 0)) if same_file else 0
        start = min(max(start, 0), len(workbook.items) - 1)
        self.workbook = workbook
        self.workbook_var.set(f"{workbook.path.name} · {workbook.sheet} · A열")
        self._refresh_list()
        index = self._next_pending(start)
        if index is None:
            index = self._next_pending(0)
        if index is None:
            self._display_item(start, search=False)
            self.status_var.set("모든 제목의 이미지가 이미 저장되어 있습니다. 목록에서 확인할 제목을 선택하세요.")
        else:
            self._display_item(index)

    def _refresh_list(self):
        self.title_list.delete(0, "end")
        if self.workbook:
            for item in self.workbook.items:
                saved = self.saved.find(item.title)
                self.title_list.insert("end", f"{'✓' if saved else '·'} {item.row:>3}  {item.title.replace('_', ' ')}")
                if saved:
                    self.title_list.itemconfigure("end", fg=ACCENT)
            if self.item_index >= 0:
                self.title_list.selection_set(self.item_index)
        self._update_batch_controls()

    def _update_batch_controls(self):
        total = len(self.workbook.items) if self.workbook else 0
        saved = sum(self.saved.find(item.title) is not None for item in self.workbook.items) if total and hasattr(self, "saved") else 0
        self.queue_var.set(f"{self.item_index + 1 if total and self.item_index >= 0 else 0} / {total} · 저장 {saved} · 남음 {total - saved}")
        locked = self.downloading or self.bulk_running
        self.previous_button.configure(state="normal" if self.item_index > 0 and not locked else "disabled")
        self.next_button.configure(state="normal" if 0 <= self.item_index < total - 1 and not locked else "disabled")
        self.title_list.configure(state="disabled" if locked else "normal")
        self.bulk_button.configure(state="normal" if total and not locked else "disabled")
        self.bulk_stop_button.configure(state="normal" if self.bulk_running and not self.bulk_stop.is_set() else "disabled")

    def _next_pending(self, start):
        if not self.workbook:
            return None
        for index in range(start, len(self.workbook.items)):
            if not self.skip_existing_var.get() or not self.saved.find(self.workbook.items[index].title):
                return index
        return None

    def _display_item(self, index, *, search=True):
        self._cancel_pending()
        self.item_index = index
        item = self.workbook.items[index]
        self.setting_title = True
        self.title_var.set(item.title)
        self.setting_title = False
        self.manual_title = True
        self.query_var.set(item.title.replace("_", " "))
        self.kind_var.set("TV + 영화")
        title = item.title.replace("_", " ")
        self.current_title_var.set(f"A{item.row} · {title[:64]}{'…' if len(title) > 64 else ''}")
        self.title_list.selection_clear(0, "end")
        self.title_list.selection_set(index)
        self.title_list.see(index)
        self.works, self.work, self.all_posters, self.posters = [], None, [], []
        self.result_combo.configure(values=())
        self.result_var.set("作品 / 작품을 검색하세요")
        self._render_posters()
        self._set_busy(False)
        self._save_settings()
        if search:
            self.search()
        self.focus_set()

    def _select_item(self, event=None):
        if self.downloading or self.bulk_running or not self.workbook:
            return
        selection = self.title_list.curselection()
        if selection and selection[0] != self.item_index:
            self._display_item(selection[0])

    def go_previous(self):
        if self.workbook and self.item_index > 0 and not self.downloading and not self.bulk_running:
            # 저장된 제목도 돌아가서 볼 수 있으며 중복 저장 방지는 계속 적용합니다.
            self._display_item(self.item_index - 1)

    def go_next(self):
        if not self.workbook or self.downloading or self.bulk_running:
            return
        self._cancel_pending()
        self.saved.refresh()
        index = self._next_pending(self.item_index + 1)
        self._refresh_list()
        if index is not None:
            self._display_item(index)
        else:
            remaining = sum(self.saved.find(item.title) is None for item in self.workbook.items)
            self.status_var.set(f"목록 끝입니다. 미저장 제목 {remaining}개는 왼쪽 목록에서 다시 선택할 수 있습니다." if remaining else
                                "완료! 엑셀의 모든 제목에 이미지가 저장되어 있습니다.")

    def manual_mode(self):
        if self.downloading or self.bulk_running:
            return
        self._cancel_pending()
        self.workbook = None
        self.item_index = -1
        self.title_list.delete(0, "end")
        self.workbook_var.set("개별 검색 모드 · 엑셀 A열 불러오기로 목록 작업 재개")
        self.current_title_var.set("포스터를 보고, 1–9번으로 저장하세요")
        self.manual_title = False
        self._set_busy(False, "검색어와 저장명을 입력한 뒤 검색하세요.")

    def _skip_changed(self):
        if self.bulk_running:
            return
        self.saved.refresh()
        self._refresh_list()
        self._save_settings()
        self.status_var.set("이미 저장한 제목을 건너뛰고 중복 다운로드를 차단합니다." if self.skip_existing_var.get() else
                            "중복 방지를 해제했습니다. 같은 제목은 (2), (3)처럼 추가 저장됩니다.")

    def select_poster(self, slot):
        if self.busy or self.downloading or self.bulk_running or not self.work or not self.client or not 0 <= slot < len(self.posters):
            return
        try:
            original_title = self.workbook.items[self.item_index].title if self.workbook else self.title_var.get()
            title = safe_title(original_title)
            self.saved.refresh()
        except (ValueError, OSError) as error:
            messagebox.showinfo("저장할 제목", self._error_message(error), parent=self)
            return
        skip = self.skip_existing_var.get()
        if skip and (existing := self.saved.find(original_title)):
            self.status_var.set(f"이미 저장되어 있어 다시 저장하지 않습니다: {existing.name}")
            return
        if self.advance_timer is not None:
            self.after_cancel(self.advance_timer)
            self.advance_timer = None
        poster, work, client = self.posters[slot], self.work, self.client
        directory = Path(self.directory_var.get())
        source = {"excel_path": str(self.workbook.path), "sheet": self.workbook.sheet,
                  "row": self.workbook.items[self.item_index].row} if self.workbook else None
        self.downloading = True
        self.cards[slot][0].configure(highlightthickness=2, highlightbackground=ACCENT)
        self._set_busy(False, f"{slot + 1}번 원본 저장 중… → {title}")
        self.focus_set()

        def download():
            try:
                path = download_poster(client, work, poster, directory, original_title, skip_existing=skip)
            except AlreadySaved as error:
                return error.path, "", False
            warning = ""
            record = history_record(work, poster, path, original_title)
            if source:
                record["source"] = source
            try:
                with (path.parent / "download_history.jsonl").open("a", encoding="utf-8") as output:
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                warning = " · 기록 저장 실패"
            return path, warning, True
        self._run(download, "download", self.generation)

    def _drain_events(self):
        if self.closed:
            return
        for _ in range(60):
            try:
                name, generation, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            if name == "bulk_update":
                self._show_bulk_update(payload)
            elif name == "bulk_done":
                self._finish_bulk(payload)
                if self.closed:
                    return
            elif name == "search":
                self.works = payload
                self._set_busy(False)
                if not self.works:
                    self.result_var.set("검색 결과 없음")
                    self.status_var.set("애니메이션 검색 결과가 없습니다. 검색어를 고치거나 ‘다음 / 건너뛰기’를 누르세요.")
                    self.focus_set()
                    continue
                self.result_combo.configure(values=[f"{work.label} [ID {work.id}]" for work in self.works])
                self.result_combo.current(0)
                self._load_work(self.works[0])
            elif name == "posters":
                self.all_posters = [poster for poster in payload if poster.language == "ja"]
                self.posters = self.all_posters[:PAGE_SIZE]
                self._set_busy(False)
                self._render_posters()
                title = self.workbook.items[self.item_index].title if self.workbook else self.title_var.get()
                existing = self.saved.find(title) if title.strip() else None
                if existing and self.skip_existing_var.get():
                    self.status_var.set(f"이미 저장한 제목입니다. 중복 저장이 차단됩니다: {existing.name}")
                elif self.posters:
                    self.status_var.set(f"일본어 포스터 {len(self.posters)}장. 1–9번으로 한 장을 저장하세요.")
                else:
                    self.status_var.set("일본어로 등록된 포스터가 없습니다. 다른 작품 후보를 고르거나 다음 제목으로 넘어가세요.")
                self.focus_set()
            elif name == "thumbnail":
                slot, path, data = payload
                self.cache[path] = data
                self.cache.move_to_end(path)
                while len(self.cache) > 128:
                    self.cache.popitem(last=False)
                self._show_thumbnail(slot, data)
            elif name == "thumbnail_error":
                self.cards[payload][1].configure(text="미리보기 실패\n번호로 원본 저장 가능")
            elif name == "download":
                self.last_saved, warning, created = payload
                self.downloading = False
                self.saved.add(self.last_saved)
                self._set_busy(False, f"{'저장 완료' if created else '이미 저장되어 건너뜀'}: {self.last_saved.name}{warning}")
                self.last_saved_var.set(f"최근 저장: {self.last_saved.name}")
                self._refresh_list()
                self.focus_set()
                if self.workbook:
                    self.advance_timer = self.after(250, self._advance_after_save)
            elif name == "error":
                self.downloading = False
                if self.bulk_running:
                    self.bulk_summary.stopped = True
                    self.bulk_summary.halt_reason = payload
                    self._finish_bulk(self.bulk_summary)
                    if self.closed:
                        return
                    continue
                self._set_busy(False, payload)
                messagebox.showerror("작업 실패", payload, parent=self)
        self.event_timer = self.after(65, self._drain_events)

    def _advance_after_save(self):
        self.advance_timer = None
        self.go_next()

    def choose_directory(self):
        if self.downloading or self.bulk_running:
            return
        directory = filedialog.askdirectory(title="포스터 저장 폴더", initialdir=self.directory_var.get(), parent=self)
        if directory:
            try:
                saved = SavedPosters(Path(directory))
            except OSError as error:
                messagebox.showerror("저장 폴더", self._error_message(error), parent=self)
                return
            self.directory_var.set(directory)
            self.saved = saved
            self._refresh_list()
            self._save_settings()

    def open_directory(self):
        try:
            directory = Path(self.directory_var.get()).expanduser().resolve()
            directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(directory))
        except OSError as error:
            messagebox.showerror("폴더 열기", self._error_message(error), parent=self)

    def edit_key(self):
        if self.busy or self.downloading or self.bulk_running:
            return
        dialog = tk.Toplevel(self)
        dialog.title("TMDB API 키 설정")
        dialog.configure(bg=PANEL, padx=22, pady=20)
        dialog.geometry("470x200")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        self._label(dialog, "TMDB API Key (v3)", anchor="w").pack(fill="x")
        value = tk.StringVar(value=self.api_key)
        entry = self._entry(dialog, value, show="●")
        entry.pack(fill="x", ipady=8, pady=10)
        self._label(dialog, "이 PC의 .env 파일에 저장됩니다.", fg=MUTED, anchor="w").pack(fill="x")

        def save():
            try:
                save_api_key(value.get())
            except (ValueError, OSError) as error:
                messagebox.showerror("API 키 저장", self._error_message(error), parent=dialog)
                return
            self.api_key = value.get().strip()
            self.client = None
            self.search_cache.clear()
            self.poster_cache.clear()
            dialog.destroy()
            self.status_var.set("API 키를 저장했습니다. 검색 버튼을 눌러 주세요.")
        self._button(dialog, "저장", save, primary=True).pack(anchor="e", pady=(10, 0))
        entry.bind("<Return>", lambda event: save())
        entry.focus_set()

    def _save_settings(self):
        value = dict(self.settings)
        value.update(directory=self.directory_var.get(), skip_existing=self.skip_existing_var.get())
        if self.workbook:
            value.update(last_excel=str(self.workbook.path), excel_index=self.item_index)
        self.settings = value
        try:
            (ROOT / "settings.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _close(self):
        if self.bulk_running:
            self.close_after_bulk = True
            self.stop_bulk()
            self.status_var.set("중지 후 창을 닫습니다. 현재 서버 요청이 끝날 때까지 기다려 주세요.")
            return
        if self.downloading:
            messagebox.showinfo("다운로드 중", "원본 저장이 끝나면 창을 닫아 주세요.", parent=self)
            return
        self.closed = True
        for timer in (self.event_timer, self.restore_timer, self.advance_timer):
            if timer is not None:
                self.after_cancel(timer)
        self._save_settings()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.destroy()
