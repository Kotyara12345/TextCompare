"""Сравнение двух текстов с подсветкой совпадений.

Портативное приложение под Windows.
Зависимости — только стандартная библиотека (tkinter, difflib, threading,
logging).

Минимальная версия Python — 3.9 (PEP 585 дженерики list[...], dict[...],
tuple[...] и Optional/Union вместо X | Y для runtime-алиасов).

Запуск:
    python text_compare.py

Сборка в один exe:
    pyinstaller --onefile --windowed --name TextCompare text_compare.py

Логи:
    %TEMP%\\text_compare.log  (Windows)
    /tmp/text_compare.log      (Linux/macOS)

Известное ограничение: SequenceMatcher внутри get_matching_blocks()
не прерывается и держит GIL. Для очень больших текстов нужен
rapidfuzz или multiprocessing — вне рамок текущей задачи.
"""

from __future__ import annotations

import logging
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from tkinter import ttk, filedialog
from typing import Callable, Optional, TypeVar, Union


# ===========================================================================
# Дженерик для типизации очередей и воркеров.
# ===========================================================================
_T = TypeVar("_T")

WorkerFn = Callable[[], None]
WorkerFactory = Callable[[int, threading.Event], WorkerFn]


# ===========================================================================
# Логирование — изолированное, не трогает root logger.
# ===========================================================================
LOG_PATH = Path(tempfile.gettempdir()) / "text_compare.log"
logger = logging.getLogger("text_compare")

#: Порог, после которого пишем warning в лог. 5 МБ — эмпирика.
LARGE_TEXT_BYTES = 5 * 1024 * 1024


def _setup_logging() -> None:
    """Настраивает ТОЛЬКО логгер приложения.

    root logger не трогаем: если модуль импортируют как часть
    большего приложения, наши настройки не перебьют чужие.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"
    ))
    logger.addHandler(handler)


# ===========================================================================
# I/O: чтение файлов с определением кодировки
# ===========================================================================
_BOMS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

_FALLBACK_WARNING = "Кодировка не распознана — символы могут быть искажены"


def read_text_file(path: Union[str, Path]) -> tuple[str, Optional[str]]:
    """Читает файл, пытаясь угадать кодировку.

    Возвращает (текст, предупреждение_или_None).

    Порядок: BOM -> utf-8 -> cp1251 с предупреждением.
    cp1251 «всеядна» — если utf-8 провалился, уверенности в результате
    нет, ставим предупреждение. utf-16 без BOM не пробуем: он молча
    «съедает» произвольные байты.
    """
    raw = Path(path).read_bytes()
    logger.info("read_text_file: %s, %d bytes", path, len(raw))

    if len(raw) > LARGE_TEXT_BYTES:
        logger.warning("read_text_file: large file %d bytes (>%.1f MB)",
                       len(raw), LARGE_TEXT_BYTES / 1024 / 1024)

    for bom, enc in _BOMS:
        if raw.startswith(bom):
            logger.info("read_text_file: detected BOM, encoding=%s", enc)
            return raw.decode(enc), None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("read_text_file: utf-8 failed, fallback cp1251")
        return raw.decode("cp1251", errors="replace"), _FALLBACK_WARNING

    logger.info("read_text_file: decoded as utf-8")
    return text, None


def _file_size_hint(path: Union[str, Path]) -> str:
    """Возвращает «(X.X МБ)» для больших файлов или пустую строку."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return ""
    if size <= 1024 * 1024:
        return ""
    return f" ({size / 1024 / 1024:.1f} МБ)"


# ===========================================================================
# Логика сравнения — не зависит от tkinter
# ===========================================================================
class _Cancelled(Exception):
    """Сигнал о том, что прогон сравнения прерван."""


Match = tuple[int, int, int, int]
TkMap = Optional[list[int]]


class TextDiffer:
    """Чистая логика сравнения текстов.

    Никаких обращений к tkinter — только строки, списки и SequenceMatcher.
    """

    MIN_MATCH: dict[str, int] = {"chars": 4, "words": 1, "lines": 1}
    _CANCEL_CHECK_EVERY = 1 << 16

    _WORD_RE = re.compile(r"\w+|\s+|[^\w\s]+", re.UNICODE)

    @classmethod
    def tokenize(cls,
                 text: str,
                 mode: str,
                 cancel: Optional[threading.Event] = None
                 ) -> list[tuple[str, int, int]]:
        """Разбивает текст на токены (token, start, end).

        Если передан cancel — раз в _CANCEL_CHECK_EVERY токенов
        проверяем его и бросаем _Cancelled.
        """
        if mode == "lines":
            tokens: list[tuple[str, int, int]] = []
            pos = 0
            count = 0
            for line in text.splitlines(keepends=True):
                if cancel is not None:
                    count += 1
                    if count >= cls._CANCEL_CHECK_EVERY:
                        if cancel.is_set():
                            raise _Cancelled()
                        count = 0
                tokens.append((line, pos, pos + len(line)))
                pos += len(line)
            if pos < len(text):
                tokens.append((text[pos:], pos, len(text)))
            return tokens

        result: list[tuple[str, int, int]] = []
        count = 0
        for m in cls._WORD_RE.finditer(text):
            if cancel is not None:
                count += 1
                if count >= cls._CANCEL_CHECK_EVERY:
                    if cancel.is_set():
                        raise _Cancelled()
                    count = 0
            result.append((m.group(), m.start(), m.end()))
        return result

    @staticmethod
    def build_tk_map(text: str) -> TkMap:
        """Строит карту py_index -> tk_index или возвращает None.

        Tk на Windows считает позиции в UTF-16 code units, Python —
        в code points. Для BMP они совпадают, разница только на
        astral-символах (эмодзи, часть CJK ext.).
        """
        if text.isascii():
            return None
        if not any(ord(ch) > 0xFFFF for ch in text):
            return None

        result: list[int] = [0] * (len(text) + 1)
        tk_pos = 0
        for i, ch in enumerate(text):
            result[i] = tk_pos
            tk_pos += 2 if ord(ch) > 0xFFFF else 1
        result[len(text)] = tk_pos
        return result

    @classmethod
    def compute_matches(cls,
                        t1: str,
                        t2: str,
                        mode: str,
                        case_sensitive: bool,
                        cancel: threading.Event) -> list[Match]:
        if cancel.is_set():
            raise _Cancelled()

        min_match = cls.MIN_MATCH[mode]

        if mode == "chars":
            if case_sensitive:
                seq1, seq2 = t1, t2
            else:
                seq1 = [ch.lower() for ch in t1]
                seq2 = [ch.lower() for ch in t2]

            matcher = SequenceMatcher(None, seq1, seq2, autojunk=False)
            return [(i, i + n, j, j + n)
                    for i, j, n in matcher.get_matching_blocks()
                    if n >= min_match]

        toks1 = cls.tokenize(t1, mode, cancel)
        toks2 = cls.tokenize(t2, mode, cancel)

        if cancel.is_set():
            raise _Cancelled()

        seq1 = [t[0] for t in toks1]
        seq2 = [t[0] for t in toks2]
        if not case_sensitive:
            seq1 = [s.lower() for s in seq1]
            seq2 = [s.lower() for s in seq2]

        # get_matching_blocks() неделим и держит GIL. Отмена срабатывает
        # до и после вызова. Для мегабайтных нагрузок — rapidfuzz или
        # multiprocessing.
        matcher = SequenceMatcher(None, seq1, seq2, autojunk=False)
        blocks = matcher.get_matching_blocks()

        if cancel.is_set():
            raise _Cancelled()

        result: list[Match] = []
        for i, j, n in blocks:
            if n < min_match:
                continue
            s1, e1 = toks1[i][1], toks1[i + n - 1][2]
            s2, e2 = toks2[j][1], toks2[j + n - 1][2]
            result.append((s1, e1, s2, e2))
        return result


# ===========================================================================
# Типизированные сообщения между воркерами и UI-потоком
# ===========================================================================
@dataclass(frozen=True)
class CompareOk:
    run_id: int
    matches: list[Match]
    len1: int
    len2: int
    map1: TkMap
    map2: TkMap


@dataclass(frozen=True)
class CompareError:
    run_id: int
    exc: Exception


@dataclass(frozen=True)
class LoadOk:
    target: str
    path: str
    content: str
    warning: Optional[str] = None


@dataclass(frozen=True)
class LoadError:
    target: str
    exc: Exception


CompareMsg = Union[CompareOk, CompareError]
LoadMsg = Union[LoadOk, LoadError]


# ===========================================================================
# GUI
# ===========================================================================
class TextComparator(ttk.Frame):
    """Виджет сравнения двух текстов с подсветкой совпавших фрагментов."""

    MATCH_TAG = "match"
    MATCH_COLOR = "#8FE38F"
    POLL_INTERVAL_MS = 60
    SHUTDOWN_TIMEOUT = 0.5
    MAX_LIVE_WORKERS = 3
    INSERT_CHUNK_CHARS = 500_000
    HUGE_TEXT_CHARS = 2_000_000

    mode: tk.StringVar
    case_sensitive: tk.BooleanVar

    def __init__(self, master: tk.Misc, **kwargs) -> None:
        super().__init__(master, **kwargs)

        self._compare_queue: queue.Queue[CompareMsg] = queue.Queue(maxsize=1)
        self._load_queue: queue.Queue[LoadMsg] = queue.Queue(maxsize=1)

        self._cancel_events: dict[int, threading.Event] = {}
        self._run_id: int = 0
        self._workers: list[tuple[threading.Thread, int]] = []

        self._load_workers: list[tuple[threading.Thread, str]] = []
        self._load_cancel_events: dict[str, threading.Event] = {}

        # Токен генерации чанкованной вставки — свой на каждую панель.
        self._insert_gens: dict[str, int] = {"text1": 0, "text2": 0}

        self._closing = False

        self._build_ui()
        self._schedule_poll()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=6)

        self.compare_btn = ttk.Button(toolbar, text="Сравнить",
                                      command=self.compare)
        self.compare_btn.pack(side=tk.LEFT, padx=2)

        self.cancel_btn = ttk.Button(toolbar, text="Отмена",
                                     command=self.cancel,
                                     state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="Очистить",
                   command=self.clear).pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="Открыть файл -> Текст 1",
                   command=lambda: self.load_file("text1")
                   ).pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="Открыть файл -> Текст 2",
                   command=lambda: self.load_file("text2")
                   ).pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="Режим:").pack(side=tk.LEFT, padx=(20, 4))
        self.mode = tk.StringVar(value="words")
        for label, value in (("По словам", "words"),
                             ("По строкам", "lines"),
                             ("По символам", "chars")):
            ttk.Radiobutton(toolbar, text=label,
                            variable=self.mode, value=value
                            ).pack(side=tk.LEFT, padx=2)

        self.case_sensitive = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="Учитывать регистр",
                        variable=self.case_sensitive
                        ).pack(side=tk.LEFT, padx=(16, 2))

        main = ttk.Frame(self)
        main.grid(row=1, column=0, sticky="nsew", padx=6)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self.text1 = self._make_pane(main, 0, "Текст 1")
        self.text2 = self._make_pane(main, 1, "Текст 2")

        for widget in (self.text1, self.text2):
            widget.tag_configure(self.MATCH_TAG, background=self.MATCH_COLOR)

        self.status = ttk.Label(self,
                                text="Введите тексты и нажмите «Сравнить»",
                                anchor="w")
        self.status.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 6))

    def _make_pane(self, parent: tk.Misc, col: int, title: str) -> tk.Text:
        frame = ttk.LabelFrame(parent, text=title)
        frame.grid(row=0, column=col, sticky="nsew",
                   padx=(0, 3) if col == 0 else (3, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(frame, wrap=tk.WORD, undo=True, font=("Consolas", 11))
        text.grid(row=0, column=0, sticky="nsew")

        sb = ttk.Scrollbar(frame, command=text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=sb.set)
        return text

    def _pane(self, target: str) -> tk.Text:
        return self.text1 if target == "text1" else self.text2

    # --------------------------------------------- безопасное обновление UI
    def _set_status(self, text: str) -> None:
        """Единая точка записи в self.status. Глотает TclError."""
        if self._closing:
            return
        try:
            self.status.config(text=text)
        except tk.TclError:
            logger.warning("_set_status: TclError, widget destroyed")

    # ----------------------------------------------------------- файлы / UX
    def load_file(self, target: str) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("Текстовые файлы", "*.txt"),
                       ("Все файлы", "*.*")])
        if not path:
            return

        self._prune_workers()

        prev = self._load_cancel_events.pop(target, None)
        if prev is not None:
            prev.set()
            logger.info("load_file: cancelled previous load for %s", target)

        cancel = threading.Event()
        self._load_cancel_events[target] = cancel

        logger.info("load_file: target=%s path=%s", target, path)
        self._set_status(f"Загрузка: {path}…{_file_size_hint(path)}")

        def worker() -> None:
            try:
                content, warning = read_text_file(path)
            except OSError as exc:
                logger.exception("load_file: OSError for %s", path)
                self._post_load(LoadError(target, exc))
                return

            if cancel.is_set():
                logger.info("load_file: %s cancelled, drop result", target)
                return

            self._post_load(LoadOk(target, path, content, warning))

        t = threading.Thread(target=worker, daemon=True,
                             name=f"load-{target}")
        self._load_workers.append((t, target))
        t.start()

    def clear(self) -> None:
        self.text1.delete("1.0", tk.END)
        self.text2.delete("1.0", tk.END)
        self._set_status("Очищено")
        logger.info("clear: text areas cleared")

    # --------------------------------------------- жизненный цикл воркеров
    def shutdown(self) -> None:
        """Останавливает все фоновые воркеры с общим дедлайном."""
        logger.info("shutdown: cancel + join up to %.1fs", self.SHUTDOWN_TIMEOUT)
        self._closing = True

        for ev in self._cancel_events.values():
            ev.set()
        self._cancel_events.clear()
        for ev in self._load_cancel_events.values():
            ev.set()
        self._load_cancel_events.clear()

        deadline = time.monotonic() + self.SHUTDOWN_TIMEOUT
        all_workers = ([w for w, _ in self._workers]
                       + [w for w, _ in self._load_workers])
        for w in all_workers:
            if not w.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("shutdown: timeout reached, %s may be alive",
                               w.name)
                break
            w.join(timeout=remaining)

        self._workers.clear()
        self._load_workers.clear()
        logger.info("shutdown: done")

    def _prune_workers(self) -> None:
        """Убирает завершившиеся воркеры и связанные с ними cancel-Event'ы.

        Вызывается только из главного потока Tkinter — гонки при
        пересборке словарей нет.
        """
        self._workers = [(w, rid) for w, rid in self._workers if w.is_alive()]
        alive_ids = {rid for _, rid in self._workers}
        self._cancel_events = {k: v for k, v in self._cancel_events.items()
                               if k in alive_ids}

        self._load_workers = [(w, tgt) for w, tgt in self._load_workers
                              if w.is_alive()]
        alive_targets = {tgt for _, tgt in self._load_workers}
        self._load_cancel_events = {
            k: v for k, v in self._load_cancel_events.items()
            if k in alive_targets
        }

    def _next_run_id(self) -> int:
        """Единственная точка изменения _run_id. Только главный поток."""
        self._run_id += 1
        return self._run_id

    def _start_compare_worker(self, factory: WorkerFactory) -> Optional[int]:
        """Атомарно проверяет capacity и запускает воркер.

        Возвращает run_id или None при исчерпании лимита. run_id
        выдаётся только при успешной регистрации.
        """
        self._prune_workers()
        if len(self._workers) >= self.MAX_LIVE_WORKERS:
            return None
        run_id = self._next_run_id()
        cancel = threading.Event()
        self._cancel_events[run_id] = cancel
        worker_fn = factory(run_id, cancel)
        t = threading.Thread(target=worker_fn, daemon=True,
                             name=f"compare-{run_id}")
        self._workers.append((t, run_id))
        t.start()
        return run_id

    # --------------------------------------------- общий финишер UI-состояния
    def _finish_work(self, status_text: str) -> None:
        """Единая точка выхода из «рабочего» режима."""
        if self._closing:
            return
        try:
            self.compare_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
            self.status.config(text=status_text)
        except tk.TclError:
            logger.warning("_finish_work: TclError, widget already destroyed")

    # ------------------------------------------------------------ сравнение
    def compare(self) -> None:
        if self._closing:
            return

        # Визуальный отклик ДО get(): на текстах > 10 МБ get() может
        # занять сотни мс. Tkinter не потокобезопасен, поэтому get()
        # обязан выполняться в главном потоке — вынести его в воркер
        # нельзя, это уронит интерпретатор.
        self._set_status("Чтение текста...")

        t1 = self.text1.get("1.0", "end-1c")
        t2 = self.text2.get("1.0", "end-1c")

        self.text1.tag_remove(self.MATCH_TAG, "1.0", tk.END)
        self.text2.tag_remove(self.MATCH_TAG, "1.0", tk.END)

        # --- Тривиальные ветки без воркера ---
        if not t1 and not t2:
            logger.info("compare: both texts empty")
            self._apply_matches([], 0, 0, None, None)
            return
        if not t1 or not t2:
            logger.info("compare: one side empty")
            self._apply_matches([], len(t1), len(t2), None, None)
            return

        case_sensitive = self.case_sensitive.get()
        mode = self.mode.get()
        total_chars = len(t1) + len(t2)

        if total_chars > self.HUGE_TEXT_CHARS:
            logger.warning("compare: huge texts, %d chars total", total_chars)
            self._set_status(
                f"Сравнение больших текстов "
                f"(~{total_chars // 1000} тыс. символов), подождите…"
            )

        def make_worker(run_id: int,
                        cancel: threading.Event) -> WorkerFn:
            def worker() -> None:
                started = time.monotonic()
                try:
                    map1 = TextDiffer.build_tk_map(t1)
                    map2 = TextDiffer.build_tk_map(t2)
                    matches = TextDiffer.compute_matches(
                        t1, t2, mode, case_sensitive, cancel,
                    )
                except _Cancelled:
                    # UI уже в рабочем состоянии: cancel() синхронно
                    # вызвал _finish_work сразу после Event.set().
                    logger.info("compare: run_id=%d cancelled after %.3fs",
                                run_id, time.monotonic() - started)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.exception("compare: run_id=%d failed", run_id)
                    self._post_compare(CompareError(run_id, exc))
                    return

                if cancel.is_set():
                    logger.debug("compare: run_id=%d cancelled, drop result",
                                 run_id)
                    return

                logger.info("compare: run_id=%d done in %.3fs, %d blocks",
                            run_id, time.monotonic() - started, len(matches))
                self._post_compare(
                    CompareOk(run_id, matches, len(t1), len(t2), map1, map2)
                )
            return worker

        run_id = self._start_compare_worker(make_worker)
        if run_id is None:
            logger.warning("compare: too many live workers (%d)",
                           len(self._workers))
            self._set_status(
                f"Уже выполняется {len(self._workers)} сравнений, "
                f"дождитесь завершения…"
            )
            return

        logger.info("compare: run_id=%d mode=%s case_sensitive=%s len1=%d len2=%d",
                    run_id, mode, case_sensitive, len(t1), len(t2))

        self.compare_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self._set_status("Сравнение...")

    def cancel(self) -> None:
        """Прерывает ВСЕ живые сравнения и возвращает UI в рабочее состояние.

        Кнопки восстанавливаются здесь синхронно — UI отзывчив сразу,
        не дожидаясь, пока воркеры дойдут до чекпоинта cancel.
        """
        active = len(self._cancel_events)
        new_run_id = self._next_run_id()
        logger.info("cancel: %d active run(s), run_id -> %d",
                    active, new_run_id)

        for ev in self._cancel_events.values():
            ev.set()
        self._cancel_events.clear()

        self._finish_work("Отменено")

    # --------------------------------------------------- очереди сообщений
    def _post_compare(self, msg: CompareMsg) -> None:
        self._put_with_evict(self._compare_queue, msg)

    def _post_load(self, msg: LoadMsg) -> None:
        self._put_with_evict(self._load_queue, msg)

    @staticmethod
    def _put_with_evict(q: queue.Queue[_T], msg: _T) -> None:
        """put_nowait; при Full — вытесняет старое и кладёт новое.

        Цикл нужен, потому что производителей может быть больше одного.
        maxsize=1 гарантирует, что очередь не растёт: не более одного
        сообщения ждёт обработки, остальные вытесняются.
        """
        while True:
            try:
                q.put_nowait(msg)
                return
            except queue.Full:
                pass
            try:
                q.get_nowait()
            except queue.Empty:
                pass

    # ------------------------------------------------------------- подсветка
    def _apply_matches(self,
                       matches: list[Match],
                       len1: int,
                       len2: int,
                       map1: TkMap,
                       map2: TkMap) -> None:
        if self._closing:
            return

        matched1 = matched2 = 0

        try:
            if matches:
                idx1: list[str] = []
                idx2: list[str] = []
                for s1, e1, s2, e2 in matches:
                    if map1 is not None:
                        s1 = map1[s1]
                        e1 = map1[e1]
                    if map2 is not None:
                        s2 = map2[s2]
                        e2 = map2[e2]
                    idx1.append(f"1.0 + {s1} chars")
                    idx1.append(f"1.0 + {e1} chars")
                    idx2.append(f"1.0 + {s2} chars")
                    idx2.append(f"1.0 + {e2} chars")
                    matched1 += e1 - s1
                    matched2 += e2 - s2

                self.text1.tag_add(self.MATCH_TAG, *idx1)
                self.text2.tag_add(self.MATCH_TAG, *idx2)
        except tk.TclError:
            logger.warning("_apply_matches: TclError, widget already destroyed")
            return

        self._finish_work(
            f"Текст 1: совпало {matched1}/{len1} симв. "
            f"({self._pct(matched1, len1):.1f}%)   |   "
            f"Текст 2: совпало {matched2}/{len2} симв. "
            f"({self._pct(matched2, len2):.1f}%)"
        )

    @staticmethod
    def _pct(part: int, total: int) -> float:
        return part / total * 100 if total else 0.0

    # ----------------------------------------- межпоточный обмен с UI-потоком
    def _schedule_poll(self) -> None:
        self.after(self.POLL_INTERVAL_MS, self._poll_results)

    def _poll_results(self) -> None:
        """Единственная точка, где главный поток забирает результаты."""
        if self._closing:
            return

        try:
            self._prune_workers()
            self._drain_compare()
            self._drain_load()
        except tk.TclError:
            logger.warning("_poll_results: TclError, stopping poll loop")
            self._closing = True
            return

        if not self._closing:
            self._schedule_poll()

    def _drain_compare(self) -> None:
        try:
            msg = self._compare_queue.get_nowait()
        except queue.Empty:
            return

        if isinstance(msg, CompareOk):
            if msg.run_id != self._run_id:
                logger.debug("drain_compare: stale ok run_id=%d", msg.run_id)
                return
            self._apply_matches(msg.matches, msg.len1, msg.len2,
                                msg.map1, msg.map2)
        elif isinstance(msg, CompareError):
            if msg.run_id != self._run_id:
                logger.debug("drain_compare: stale err run_id=%d", msg.run_id)
                return
            self._finish_work(f"Ошибка сравнения: {msg.exc}")

    def _drain_load(self) -> None:
        try:
            msg = self._load_queue.get_nowait()
        except queue.Empty:
            return

        if self._closing:
            return

        widget = self._pane(msg.target)
        if isinstance(msg, LoadOk):
            self._insert_chunked(widget, msg.content, msg.path, msg.target)
            if msg.warning:
                self._set_status(f"Загружен: {msg.path} — {msg.warning}")
        elif isinstance(msg, LoadError):
            self._set_status(f"Не удалось открыть файл: {msg.exc}")

    # --------------------------------------------------- вставка в tk.Text
    def _insert_chunked(self,
                        widget: tk.Text,
                        content: str,
                        path: str,
                        target: str) -> None:
        """Вставляет текст порциями, чтобы не морозить UI на больших файлах.

        Токен генерации — свой на каждый target: загрузка в одну панель
        не должна обрывать незавершённую вставку в другую.
        """
        self._insert_gens[target] += 1
        gen = self._insert_gens[target]
        total = len(content)

        try:
            widget.delete("1.0", tk.END)
        except tk.TclError:
            logger.warning("_insert_chunked: widget gone, drop insert")
            return

        if total <= self.INSERT_CHUNK_CHARS:
            try:
                widget.insert(tk.END, content)
            except tk.TclError:
                logger.warning("_insert_chunked: widget gone, drop insert")
                return
            self._set_status(f"Загружен: {path}")
            return

        logger.info("_insert_chunked: %s %d chars in chunks of %d",
                    target, total, self.INSERT_CHUNK_CHARS)

        def insert_next(offset: int) -> None:
            if self._closing or gen != self._insert_gens[target]:
                return

            chunk = content[offset:offset + self.INSERT_CHUNK_CHARS]
            try:
                widget.insert(tk.END, chunk)
            except tk.TclError:
                logger.warning("_insert_chunked: widget gone mid-insert")
                return

            next_offset = offset + len(chunk)
            if next_offset < total:
                self.after(1, insert_next, next_offset)
            else:
                self._set_status(f"Загружен: {path}")

        insert_next(0)


# ===========================================================================
# Точка входа
# ===========================================================================
def _enable_dpi_awareness() -> None:
    """DPI-awareness с каскадным откатом для старых Windows.

    Порядок: PMv2 (Win10 1703+) -> per-monitor (Win8.1+) -> system.
    Широкий except оправдан: перебираем доступные API и останавливаемся
    на первом, который сработал.
    """
    try:
        from ctypes import windll
    except Exception:
        return

    try:
        windll.user32.SetProcessDpiAwarenessContext(-4)
        return
    except Exception:
        pass

    try:
        windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass

    try:
        windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def main() -> None:
    _setup_logging()
    logger.info("=== text_compare started, log=%s ===", LOG_PATH)

    _enable_dpi_awareness()

    root = tk.Tk()
    root.title("Сравнение текстов")
    root.geometry("1200x700")
    root.minsize(800, 500)

    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")

    comparator = TextComparator(root)
    comparator.pack(fill=tk.BOTH, expand=True)

    def on_close() -> None:
        comparator.shutdown()
        root.destroy()
        logger.info("=== text_compare stopped ===")

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()