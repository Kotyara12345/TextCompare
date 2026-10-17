"""Сравнение двух текстов с подсветкой совпадений и различий.

Портативное приложение под Windows.
Зависимости — только стандартная библиотека (tkinter, difflib, threading,
logging, json).

Минимальная версия Python — 3.9 (PEP 585 дженерики list[...], dict[...],
tuple[...] и Optional/Union вместо X | Y для runtime-алиасов).

Возможности:
    * режимы сравнения по словам/строкам/символам, учёт регистра;
    * подсветка совпадений (зелёный) и различий (красный), поиск Ctrl+F,
      переходы к различиям Alt+↓/Alt+↑, синхронная прокрутка, зум шрифта
      Ctrl+колесо;
    * Ctrl+X/C/V/A/Z/Y работают и при нелатинской раскладке (заплатка
      поведения Tk до 8.6.15 на уровне приложения);
    * меню и шорткаты в любой раскладке (Ctrl+O/S/F, F5, Ctrl+Enter);
    * экспорт отчёта в HTML, сохранение панелей в файл, обмен панелей;
    * настройки и последний каталог — рядом с exe или в %APPDATA%.

Запуск:
    python text_compare.py

Сборка в один exe:
    pyinstaller --onefile --windowed --name TextCompare text_compare.py

Тесты:
    python -m unittest discover -s tests -v

Логи:
    %TEMP%\\text_compare.log  (Windows; ротация по 1 МБ)
    /tmp/text_compare.log      (Linux/macOS)

Известные ограничения:
    * SequenceMatcher внутри get_matching_blocks() не прерывается и держит
      GIL. Поэтому гиперчастые токены (пробелы, повторы строк) исключаются
      из сопоставления, а совпавшие промежутки вокруг них сшиваются
      обратно: это лечит многоминутные простои difflib на обычных текстах,
      но тонкие различия внутри таких токенов могут не подсвечиваться.
      Для мегабайтных нагрузок по-прежнему нужен rapidfuzz или
      multiprocessing — вне рамок текущей задачи.
    * Применение тегов в Tk нелинейно дорого на очень длинных строках
      (minified-тексты). Поэтому подсветка применяется порциями с прогрессом
      и возможностью отмены, но на экстремальных объёмах может занять
      заметное время.
"""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import ttk, filedialog
from types import TracebackType
from typing import Any, Callable, Iterable, Optional, TypeVar, Union


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

#: Ротация лога: один архив по 1 МБ — лог не растёт бесконечно.
LOG_ROTATE_BYTES = 1_000_000

#: Порог, после которого пишем warning в лог. 5 МБ — эмпирика.
LARGE_TEXT_BYTES = 5 * 1024 * 1024


def _setup_logging() -> None:
    """Настраивает ТОЛЬКО логгер приложения.

    root logger не трогаем: если модуль импортируют как часть
    большего приложения, наши настройки не перебьют чужие.
    Недоступный файл лога не должен ронять запуск — в этом случае
    приложение работает без лога.
    """
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return
    try:
        handler: logging.Handler = RotatingFileHandler(
            LOG_PATH, maxBytes=LOG_ROTATE_BYTES, backupCount=1,
            encoding="utf-8")
    except OSError:
        logger.addHandler(logging.NullHandler())
        return
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
# Настройки и обработка фатальных ошибок.
#
# Портативность: настройки ищутся рядом с exe/скриптом, при отсутствии
# прав на запись — в %APPDATA%\TextCompare.
# ===========================================================================
_SETTINGS_NAME = "text_compare.json"


def _settings_dirs() -> list[Path]:
    """Каталоги-кандидаты для файла настроек, в порядке приоритета."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    else:
        dirs.append(Path(__file__).resolve().parent)
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "TextCompare")
    return dirs


def load_settings() -> dict[str, object]:
    """Читает первый найденный файл настроек; при любой ошибке — {}."""
    for base in _settings_dirs():
        path = base / _SETTINGS_NAME
        try:
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            logger.warning("load_settings: broken settings at %s", path)
            continue
        if isinstance(data, dict):
            logger.info("load_settings: loaded %s", path)
            return {str(key): value for key, value in data.items()}
    return {}


def save_settings(values: dict[str, object]) -> None:
    """Пишет настройки в первый доступный на запись каталог."""
    payload = json.dumps(values, ensure_ascii=False, indent=1)
    for base in _settings_dirs():
        path = base / _SETTINGS_NAME
        try:
            base.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                fh.write(payload)
        except OSError:
            logger.warning("save_settings: cannot write %s", path)
            continue
        logger.info("save_settings: saved %s", path)
        return


def _show_fatal_error(message: str) -> None:
    """Показывает MessageBoxW, если он доступен; молча выходит иначе."""
    try:
        from ctypes import windll
        windll.user32.MessageBoxW(None, message, "Сравнение текстов", 0x10)
    except Exception:
        logger.warning("_show_fatal_error: MessageBoxW unavailable")


def _install_error_hooks(root: tk.Tk) -> None:
    """Направляет необработанные исключения в лог.

    В windowed-сборке stderr отсутствует, поэтому исключения из колбэков
    Tk (по умолчанию печатаются в stderr) исчезают бесследно.
    """
    def on_tk_error(exc: type[BaseException], val: BaseException,
                    tb: Optional[TracebackType]) -> None:
        logger.error("необработанное исключение в колбэке Tk",
                     exc_info=(exc, val, tb))

    root.report_callback_exception = on_tk_error

    def on_uncaught(exc_type: type[BaseException], exc: BaseException,
                    tb: Optional[TracebackType]) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.error("необработанное исключение", exc_info=(exc_type, exc, tb))

    sys.excepthook = on_uncaught


# ===========================================================================
# Логика сравнения — не зависит от tkinter
# ===========================================================================
class _Cancelled(Exception):
    """Сигнал о том, что прогон сравнения прерван."""


Match = tuple[int, int, int, int]
TkMap = Optional[list[int]]


def _percent(part: int, total: int) -> float:
    """Доля part/total в процентах; 0.0 при пустом total."""
    return part / total * 100 if total else 0.0


def _complement_ranges(spans: list[tuple[int, int]],
                       total: int) -> list[tuple[int, int]]:
    """Дополнение отсортированных непересекающихся интервалов до [0, total).

    Это и есть различия: всё, что не попало ни в один блок совпадений.
    """
    gaps: list[tuple[int, int]] = []
    pos = 0
    for start, end in spans:
        if start > pos:
            gaps.append((pos, start))
        pos = max(pos, end)
    if pos < total:
        gaps.append((pos, total))
    return gaps


def _char_offset(widget: tk.Text, index: str) -> int:
    """Позиция index в «tk-символах» от начала текста (0 при ошибке)."""
    try:
        raw = widget.count("1.0", index, "chars")
    except tk.TclError:
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, tuple) and raw and isinstance(raw[0], int):
        return raw[0]
    return 0


def _render_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """HTML-текст с <span class="match"> вокруг указанных интервалов."""
    parts: list[str] = []
    pos = 0
    for start, end in spans:
        if start > pos:
            parts.append(html.escape(text[pos:start]))
        parts.append('<span class="match">'
                     + html.escape(text[start:end]) + "</span>")
        pos = end
    parts.append(html.escape(text[pos:]))
    return "".join(parts)


def build_html_report(t1: str, t2: str, matches: list[Match]) -> str:
    """Строит автономный HTML-отчёт с подсветкой совпавших фрагментов."""
    spans1 = [(s1, e1) for s1, e1, _, _ in matches]
    spans2 = [(s2, e2) for _, _, s2, e2 in matches]
    matched1 = sum(e - s for s, e in spans1)
    matched2 = sum(e - s for s, e in spans2)
    stats = (
        f"Текст 1: совпало {matched1}/{len(t1)} симв. "
        f"({_percent(matched1, len(t1)):.1f}%), "
        f"Текст 2: совпало {matched2}/{len(t2)} симв. "
        f"({_percent(matched2, len(t2)):.1f}%)."
    )
    return (
        '<!DOCTYPE html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        "<title>Сравнение текстов</title>\n<style>\n"
        "body{font-family:Consolas,monospace;background:#fafafa;color:#222;}\n"
        "h1{font-size:18px;} h2{font-size:14px;}\n"
        ".stats{color:#555;margin-bottom:12px;}\n"
        "pre{white-space:pre-wrap;word-break:break-word;background:#fff;"
        "border:1px solid #ddd;padding:8px;margin:8px 0 24px;}\n"
        ".match{background:#8FE38F;}\n"
        "</style>\n</head>\n<body>\n"
        "<h1>Сравнение текстов</h1>\n"
        f'<div class="stats">{stats}</div>\n'
        "<h2>Текст 1</h2>\n<pre>" + _render_spans(t1, spans1)
        + "</pre>\n<h2>Текст 2</h2>\n<pre>" + _render_spans(t2, spans2)
        + "</pre>\n</body>\n</html>\n"
    )


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

        if t1 and t1 == t2:
            # Частный случай: тексты идентичны — совпадение целиком.
            # Заодно прикрывает вырожденный случай, когда при
            # junk-фильтрации сжимается всё содержимое текста.
            return [(0, len(t1), 0, len(t2))]

        min_match = cls.MIN_MATCH[mode]

        if mode == "chars":
            if case_sensitive:
                matcher = SequenceMatcher(None, t1, t2, autojunk=False)
            else:
                matcher = SequenceMatcher(
                    None,
                    [ch.lower() for ch in t1],
                    [ch.lower() for ch in t2],
                    autojunk=False)
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
        #
        # Частые токены (пробелы, повторы строк) исключаем из
        # сопоставления: с ними difflib квадратично считает совпадения
        # и сканирует промежутки между соседними блоками. Сопоставляем
        # «сжатые» последовательности, а промежутки возвращаем сшивкой
        # блоков — когда текст по обе стороны совпал.
        junk, cost_before, cost_after = cls._choose_junk(seq1, seq2)
        keep1: Optional[list[int]] = None
        keep2: Optional[list[int]] = None
        if junk:
            logger.info("compute_matches: %d частых токенов исключены из "
                        "сопоставления (стоимость %d -> %d, режим=%s)",
                        len(junk), cost_before, cost_after, mode)
            keep1 = [k for k, tok in enumerate(seq1) if tok not in junk]
            keep2 = [k for k, tok in enumerate(seq2) if tok not in junk]
            seq1 = [seq1[k] for k in keep1]
            seq2 = [seq2[k] for k in keep2]

        matcher = SequenceMatcher(None, seq1, seq2, autojunk=False)
        blocks = matcher.get_matching_blocks()

        if cancel.is_set():
            raise _Cancelled()

        result: list[Match] = []
        for i, j, n in blocks:
            if n < min_match:
                continue
            spans: Iterable[Match]
            if keep1 is None or keep2 is None:
                # Без junk соседние токены блока стыкуются вплотную —
                # весь промежуток совпадает целиком.
                spans = [(toks1[i][1], toks1[i + n - 1][2],
                          toks2[j][1], toks2[j + n - 1][2])]
            else:
                # Между «сжатыми» токенами был junk: выдаём каждый токен
                # отдельно, стыки проверит сшивка ниже.
                spans = ((toks1[keep1[i + k]][1], toks1[keep1[i + k]][2],
                          toks2[keep2[j + k]][1], toks2[keep2[j + k]][2])
                         for k in range(n))
            for s1, e1, s2, e2 in spans:
                if result:
                    p1, pe1, p2, pe2 = result[-1]
                    if t1[pe1:s1] == t2[pe2:s2]:
                        result[-1] = (p1, e1, p2, e2)
                        continue
                result.append((s1, e1, s2, e2))

        if result:
            # Края текста тоже сшиваем, если они совпали: иначе
            # ведущие/хвостовые пробелы светились бы как различия.
            s1, e1, s2, e2 = result[0]
            if t1[:s1] == t2[:s2]:
                result[0] = (0, e1, 0, e2)
            s1, e1, s2, e2 = result[-1]
            if t1[e1:] == t2[e2:]:
                result[-1] = (s1, len(t1), s2, len(t2))
        return result

    #: Порог оценки стоимости поиска блоков difflib. Выше него самые
    #: «тяжёлые» токены исключаются из сопоставления (см. _choose_junk).
    MATCH_COST_LIMIT = 150_000

    @classmethod
    def _choose_junk(cls, seq1: list[str],
                     seq2: list[str]) -> tuple[set[str], int, int]:
        """Выбирает токены, из-за которых difflib уходит в квадратичность.

        Стоимость find_longest_match растёт примерно как
        Σ count1(t) * count2(t) по общим токенам: гиперчастые токены
        (пробелы, повторы строк) вешают SequenceMatcher на минуты
        (замеры: 150 тыс. -> ~0.7 с, 640 тыс. -> ~6 с, 9 млн -> >10 мин).
        Такие токены объявляются junk — они выпадают из сопоставления,
        но соседние блоки сшиваются, если текст между ними совпал.

        Возвращает (junk, стоимость_до, стоимость_после).
        """
        counts1 = Counter(seq1)
        counts2 = Counter(seq2)
        contributions: list[tuple[int, str]] = [
            (cnt * counts2[tok], tok)
            for tok, cnt in counts1.items() if tok in counts2
        ]
        total = sum(contribution for contribution, _ in contributions)
        if total <= cls.MATCH_COST_LIMIT:
            return set(), total, total
        contributions.sort(reverse=True)
        junk: set[str] = set()
        remaining = total
        for contribution, tok in contributions:
            if remaining <= cls.MATCH_COST_LIMIT:
                break
            junk.add(tok)
            remaining -= contribution
        return junk, total, remaining


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
    #: Поколение содержимого на момент запуска: если пользователь правил
    #: текст, пока воркер считал, результат применять нельзя.
    epoch: int


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
# Шорткаты Ctrl+X/C/V/A/Z/Y при нелатинской раскладке.
#
# Tk до 8.6.15 включительно определяет keysym по символу, который текущая
# раскладка назначает клавише: для Ctrl+V в русской раскладке это уже не
# «v», а NoSymbol («??»), поэтому биндинг <Control-v> не совпадает и
# виртуальное событие <<Paste>> не срабатывает — вставка из буфера молча
# ничего не делает при переключённой раскладке. Та же участь у <<Copy>>,
# <<Cut>>, <<SelectAll>>, <<Undo>> и <<Redo>>. В самом Tk заплатка
# появилась только после релиза 8.6.15 (bug 433261290b); здесь её
# поведение воспроизводится на уровне приложения: нелатинская клавиша при
# нажатом Ctrl транслируется в латинскую по физической клавише, после чего
# штатному биндингу Tk отправляется то же виртуальное событие.
# ===========================================================================
_CTRL_MASK = 0x0004
_SHIFT_MASK = 0x0001
#: Бит Alt в event.state: на Windows это Mod2 (AltGr ставит Control+Mod2),
#: на X11 — обычно Mod1 (AltGr занимает Mod5).
_ALT_MASK = 0x0010 if sys.platform == "win32" else 0x0008

#: Стандартные Ctrl-шорткаты Tk: латинская буква -> виртуальное событие.
_CTRL_SHORTCUTS: dict[str, str] = {
    "a": "<<SelectAll>>",
    "c": "<<Copy>>",
    "v": "<<Paste>>",
    "x": "<<Cut>>",
    "y": "<<Redo>>",
    "z": "<<Undo>>",
}

#: Нелатинские keysym -> латинская клавиша по позиции на QWERTY. Нужно
#: там, где нет виртуальных кодов (X11/macOS); на Windows раскладка
#: определяется по event.keycode, и таблица не участвует.
_NONLATIN_KEYSYMS: dict[str, str] = {
    # русская, украинская, белорусская (ЙЦУКЕН)
    "cyrillic_ef": "a",
    "cyrillic_es": "c",
    "cyrillic_em": "v",
    "cyrillic_che": "x",
    "cyrillic_en": "y",
    "cyrillic_ya": "z",
    # греческая
    "greek_alpha": "a",
    "greek_psi": "c",
    "greek_omega": "v",
    "greek_chi": "x",
    "greek_upsilon": "y",
    "greek_zeta": "z",
}


def _latin_letter(keycode: int, keysym: str) -> Optional[str]:
    """Возвращает латинскую букву для нелатинской клавиши или None."""
    if sys.platform == "win32":
        # На Windows keycode события — виртуальный код VK_*; для букв он
        # совпадает с ASCII-кодом заглавной латинской буквы, поэтому
        # сопоставление не зависит ни от раскладки, ни от её вида.
        if ord("A") <= keycode <= ord("Z"):
            return chr(keycode).lower()
        return None
    return _NONLATIN_KEYSYMS.get(keysym.lower())


def _event_state(event: tk.Event) -> int:
    """event.state как int: конструированные события допускают строку."""
    state = event.state
    if isinstance(state, str):
        return int(state, 0)
    return int(state)


def _ctrl_letter(event: tk.Event, shift: bool) -> Optional[str]:
    """Латинская буква для Ctrl+<клавиша> или None.

    Требуемое состояние Shift задаёт вызывающий: штатным шорткатам Tk он
    запрещён, а общеоконным действиям (Ctrl+S / Ctrl+Shift+S) — часть
    команды. Alt запрещён всегда: AltGr вводит символ, а не шорткат.
    """
    state = _event_state(event)
    if not state & _CTRL_MASK or state & _ALT_MASK:
        return None
    if bool(state & _SHIFT_MASK) != shift:
        return None

    keysym = event.keysym
    if len(keysym) == 1 and keysym.isascii():
        return keysym.lower()
    if 0 < event.keysym_num <= 0xFF:
        # Печатаемый символ Latin-1 — например, «©» от AltGr+<клавиша>
        # в немецкой раскладке. Это ввод символа, а не шорткат (та же
        # проверка есть в апстримной заплатке Tk).
        return None
    return _latin_letter(event.keycode, keysym)


def _shortcut_for_event(event: tk.Event) -> Optional[str]:
    """Возвращает виртуальное событие Tk для Ctrl-шортката или None.

    Перевод выполняется только для нелатинских раскладок: у латинской
    keysym — ASCII-символ, и штатные биндинги Tk сработают сами, дублировать
    их нельзя.
    """
    if len(event.keysym) == 1 and event.keysym.isascii():
        return None
    letter = _ctrl_letter(event, shift=False)
    if letter is None:
        return None
    return _CTRL_SHORTCUTS.get(letter)


# ===========================================================================
# GUI
# ===========================================================================
class TextComparator(ttk.Frame):
    """Виджет сравнения двух текстов с подсветкой совпавших фрагментов."""

    MATCH_TAG = "match"
    DIFF_TAG = "diff"
    FIND_TAG = "found"
    FLASH_TAG = "flash"
    MATCH_COLOR = "#8FE38F"
    DIFF_COLOR = "#FFB3B3"
    FIND_COLOR = "#FFE066"
    FLASH_COLOR = "#FFB84D"
    DEFAULT_FONT_SIZE = 11
    ZOOM_MIN = 6
    ZOOM_MAX = 40
    POLL_INTERVAL_MS = 60
    SHUTDOWN_TIMEOUT = 0.5
    MAX_LIVE_WORKERS = 3
    INSERT_CHUNK_CHARS = 500_000
    HUGE_TEXT_CHARS = 2_000_000
    #: Посимвольное сравнение квадратично по стоимости difflib: ~5 тыс.
    #: символов — секунда, 30 тыс. — десятки секунд. Выше порога —
    #: предупреждение в статусе.
    CHARS_WARN_CHARS = 30_000
    #: Подсветка применяется порциями: стартовый размер, границы и бюджет
    #: времени на одну порцию (адаптация — по факту затраченного времени).
    HIGHLIGHT_START_CHUNK = 128
    HIGHLIGHT_MIN_CHUNK = 8
    HIGHLIGHT_MAX_CHUNK = 1024
    HIGHLIGHT_BUDGET_S = 0.03
    HIGHLIGHT_WARN_BLOCKS = 5000
    #: Ограничение длины маркера при переходе к различию (Alt+↓/↑).
    FLASH_MAX_CHARS = 200

    mode: tk.StringVar
    case_sensitive: tk.BooleanVar

    def __init__(self, master: tk.Misc, **kwargs: Any) -> None:
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

        # Поколение содержимого: любая правка (в т.ч. программная) делает
        # результаты сравнения неактуальными.
        self._content_epoch: int = 0
        # Поколение подсветки: смена токена мгновенно гасит текущую
        # порционную раскраску.
        self._highlight_gen: int = 0
        self._highlight_active = False
        self._highlight_running = False
        self._last_matches: Optional[list[Match]] = None
        self._diff1: list[tuple[int, int]] = []
        self._diff2: list[tuple[int, int]] = []
        #: Счётчик > 0 — идут программные правки (вставка, очистка):
        #: событие <<Modified>> не должно считаться правкой пользователя.
        self._suppress_modified = 0
        self._sync_busy = False
        self._focused_target = "text1"
        #: Последний каталог диалогов открытия/сохранения.
        self._last_dir: Optional[str] = None

        self.sync_scroll = tk.BooleanVar(master=self, value=True)
        self.show_diffs = tk.BooleanVar(master=self, value=True)
        self.font = tkfont.Font(self, family="Consolas",
                                size=self.DEFAULT_FONT_SIZE)

        self._closing = False

        self._build_ui()
        self._schedule_poll()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

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

        ttk.Button(toolbar, text="⇄ Поменять",
                   command=self.swap_panes).pack(side=tk.LEFT, padx=2)

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
        main.grid(row=2, column=0, sticky="nsew", padx=6)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        self.text1, self.sb1 = self._make_pane(main, 0, "Текст 1")
        self.text2, self.sb2 = self._make_pane(main, 1, "Текст 2")
        self._wire_sync_scroll()

        for widget in (self.text1, self.text2):
            widget.tag_configure(self.MATCH_TAG, background=self.MATCH_COLOR)
            widget.tag_configure(self.DIFF_TAG, background=self.DIFF_COLOR)
            widget.tag_configure(self.FIND_TAG, background=self.FIND_COLOR)
            widget.tag_configure(self.FLASH_TAG, background=self.FLASH_COLOR)

        self._build_find_bar()

        self.status = ttk.Label(self,
                                text="Введите тексты и нажмите «Сравнить»",
                                anchor="w")
        self.status.grid(row=3, column=0, sticky="ew", padx=6, pady=(0, 6))

    def _build_find_bar(self) -> None:
        """Панель поиска по активной панели (Ctrl+F). Скрыта по умолчанию."""
        bar = ttk.Frame(self)
        self.find_bar = bar
        bar.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))

        ttk.Label(bar, text="Найти:").pack(side=tk.LEFT)
        self.find_var = tk.StringVar()
        self.find_entry = ttk.Entry(bar, textvariable=self.find_var, width=40)
        self.find_entry.pack(side=tk.LEFT, padx=4)
        self.find_entry.bind("<Return>", self._find_handler(forward=True))
        self.find_entry.bind("<Shift-Return>", self._find_handler(forward=False))
        self.find_entry.bind("<Escape>", self._on_find_escape)

        ttk.Button(bar, text="Далее",
                   command=lambda: self.find_next(True)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Назад",
                   command=lambda: self.find_next(False)).pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Закрыть",
                   command=self.close_find).pack(side=tk.LEFT, padx=2)
        bar.grid_remove()

    def _make_pane(self, parent: tk.Misc, col: int,
                   title: str) -> tuple[tk.Text, ttk.Scrollbar]:
        frame = ttk.LabelFrame(parent, text=title)
        frame.grid(row=0, column=col, sticky="nsew",
                   padx=(0, 3) if col == 0 else (3, 0))
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(frame, wrap=tk.WORD, undo=True, font=self.font)
        text.grid(row=0, column=0, sticky="nsew")
        text.bind("<Control-KeyPress>", self._on_ctrl_shortcut, add="+")
        text.bind(
            "<Control-MouseWheel>",
            lambda event: self._on_zoom(
                event, 1 if getattr(event, "delta", 0) > 0 else -1),
            add="+")
        text.bind("<Control-Button-4>",
                  lambda event: self._on_zoom(event, 1), add="+")
        text.bind("<Control-Button-5>",
                  lambda event: self._on_zoom(event, -1), add="+")
        text.bind("<<Modified>>", self._on_text_modified, add="+")
        text.bind("<Alt-Down>",
                  lambda event: self._jump_diff(event.widget, True), add="+")
        text.bind("<Alt-Up>",
                  lambda event: self._jump_diff(event.widget, False), add="+")
        text.bind("<FocusIn>", self._on_pane_focus, add="+")

        sb = ttk.Scrollbar(frame, command=text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=sb.set)
        return text, sb

    def _pane(self, target: str) -> tk.Text:
        return self.text1 if target == "text1" else self.text2

    def _on_pane_focus(self, event: tk.Event) -> None:
        """Запоминает панель с фокусом: поиск идёт по ней."""
        widget = event.widget
        if widget is self.text1:
            self._focused_target = "text1"
        elif widget is self.text2:
            self._focused_target = "text2"

    def _wire_sync_scroll(self) -> None:
        """Связывает прокрутку панелей: за кем тянут, за тем и вторая."""
        def make_cmd(sb: ttk.Scrollbar, source: tk.Text,
                     target: tk.Text) -> Callable[[float, float], None]:
            def cmd(first: float, last: float) -> None:
                sb.set(first, last)
                self._sync_other(source, target)
            return cmd

        self.text1.configure(yscrollcommand=make_cmd(
            self.sb1, self.text1, self.text2))
        self.text2.configure(yscrollcommand=make_cmd(
            self.sb2, self.text2, self.text1))

    def _sync_other(self, source: tk.Text, target: tk.Text) -> None:
        """Подтягивает вторую панель к доле прокрутки первой."""
        if not self.sync_scroll.get() or self._sync_busy:
            return
        self._sync_busy = True
        try:
            target.yview_moveto(source.yview()[0])
        except tk.TclError:
            pass
        finally:
            self._sync_busy = False

    def _on_ctrl_shortcut(self, event: tk.Event) -> Optional[str]:
        """<Control-KeyPress>: дублирует шорткаты Tk для нелатинской раскладки.

        Возвращает "break", чтобы классовый биндинг Tk не обработал то же
        событие вторым (на версиях Tk без заплатки он и не смог бы).
        """
        virtual = _shortcut_for_event(event)
        if virtual is None:
            return None
        try:
            event.widget.event_generate(virtual)
        except tk.TclError:
            logger.warning("_on_ctrl_shortcut: TclError on %s", virtual)
            return None
        logger.info("_on_ctrl_shortcut: %s emulated, keysym=%r keycode=%s",
                    virtual, event.keysym, event.keycode)
        return "break"

    def on_root_ctrl_key(self, event: tk.Event) -> Optional[str]:
        """Ctrl+O/S/F на уровне окна — работает в любой раскладке.

        Биндится на toplevel: срабатывает для любой панели, в том числе
        при нелатинской раскладке (буква определяется по физической клавише).
        """
        state = _event_state(event)
        if not state & _CTRL_MASK or state & _ALT_MASK:
            return None
        shift = bool(state & _SHIFT_MASK)
        keysym = event.keysym
        if len(keysym) == 1 and keysym.isascii():
            letter: Optional[str] = keysym.lower()
        elif 0 < event.keysym_num <= 0xFF:
            return None
        else:
            letter = _latin_letter(event.keycode, keysym)
        if letter == "o":
            self.load_file("text2" if shift else "text1")
            return "break"
        if letter == "s":
            self.save_pane("text2" if shift else "text1")
            return "break"
        if letter == "f" and not shift:
            self.toggle_find()
            return "break"
        return None

    def escape_action(self) -> None:
        """Esc: закрыть поиск, иначе отменить сравнение/подсветку."""
        if self.find_bar.winfo_ismapped():
            self.close_find()
            return
        if self._cancel_events or self._highlight_running:
            self.cancel()

    # ------------------------------------------------------ зум и шрифт --
    def _on_zoom(self, event: tk.Event, delta: int) -> str:
        """Ctrl+колесо: размер шрифта обеих панелей (event не используется)."""
        del event
        self.zoom(delta)
        return "break"

    def zoom(self, delta: int) -> None:
        """Меняет размер шрифта панелей на delta пунктов (с зажимом)."""
        try:
            size = int(self.font.cget("size"))
        except (TypeError, ValueError, tk.TclError):
            return
        size = max(self.ZOOM_MIN, min(self.ZOOM_MAX, size + delta))
        if size == self.font.cget("size"):
            return
        try:
            self.font.configure(size=size)
        except tk.TclError:
            return
        self._set_status(f"Размер шрифта: {size}")
        logger.info("zoom: font size -> %d", size)

    # ------------------------------------------------- правка текста --
    def _on_text_modified(self, event: tk.Event) -> None:
        """<<Modified>>: правка делает старые результаты неактуальными.

        Флаг сбрасывается всегда, иначе следующая правка не сгенерирует
        событие; программные вставки (загрузка файла, «Очистить») помечены
        счётчиком _suppress_modified и игнорируются.
        """
        widget = event.widget
        if not isinstance(widget, tk.Text):
            return
        try:
            modified = bool(widget.edit_modified())
            if modified:
                widget.edit_modified(False)
        except tk.TclError:
            return
        if not modified or self._suppress_modified:
            return
        self._invalidate_after_edit()

    def _invalidate_after_edit(self) -> None:
        """Сбрасывает результаты, ставшие неактуальными после правки."""
        if self._closing:
            return
        self._content_epoch += 1
        self._last_matches = None
        self._diff1 = []
        self._diff2 = []
        for cancel in self._cancel_events.values():
            cancel.set()
        self._cancel_events.clear()
        self._highlight_gen += 1
        if self._highlight_active:
            self._clear_highlight()
        self._finish_work("Текст изменён — нажмите «Сравнить»")

    def _restore_idle_buttons(self) -> None:
        """Возвращает кнопки в состояние покоя (без смены статуса)."""
        if self._closing:
            return
        try:
            self.compare_btn.config(state=tk.NORMAL)
            self.cancel_btn.config(state=tk.DISABLED)
        except tk.TclError:
            logger.warning("_restore_idle_buttons: TclError")

    def _clear_highlight(self) -> None:
        """Снимает подсветку совпадений и различий с обеих панелей."""
        self._highlight_active = False
        self._highlight_running = False
        for widget in (self.text1, self.text2):
            try:
                widget.tag_remove(self.MATCH_TAG, "1.0", tk.END)
                widget.tag_remove(self.DIFF_TAG, "1.0", tk.END)
            except tk.TclError:
                logger.warning("_clear_highlight: TclError")
                return

    # ---------------------------------------------------------- поиск --
    def _find_handler(self, forward: bool) -> Callable[[tk.Event], str]:
        def handler(event: tk.Event) -> str:
            del event
            self.find_next(forward)
            return "break"
        return handler

    def _on_find_escape(self, event: tk.Event) -> str:
        del event
        self.close_find()
        return "break"

    def toggle_find(self) -> None:
        """Ctrl+F: показать/скрыть панель поиска."""
        if self.find_bar.winfo_ismapped():
            self.close_find()
            return
        self.find_bar.grid()
        self.find_entry.focus_set()
        self.find_entry.select_range(0, tk.END)

    def close_find(self) -> None:
        """Скрывает панель поиска и снимает его подсветку."""
        self.find_bar.grid_remove()
        for widget in (self.text1, self.text2):
            try:
                widget.tag_remove(self.FIND_TAG, "1.0", tk.END)
            except tk.TclError:
                return
        self._pane(self._focused_target).focus_set()

    def find_next(self, forward: bool) -> str:
        """Ищет по активной панели, подсвечивает вхождение и переходит."""
        needle = self.find_var.get()
        widget = self._pane(self._focused_target)
        if not needle:
            return "break"
        idx: Optional[str] = None
        try:
            cursor = widget.index(tk.INSERT)
            if forward:
                idx = widget.search(needle, cursor, stopindex=tk.END)
                if not idx:
                    idx = widget.search(needle, "1.0", stopindex=tk.END)
                if idx:
                    widget.mark_set(tk.INSERT,
                                    f"{idx} + {len(needle)} chars")
            else:
                idx = widget.search(needle, cursor, stopindex="1.0",
                                    backwards=True)
                if not idx:
                    idx = widget.search(needle, "end-1c", stopindex="1.0",
                                        backwards=True)
                if idx:
                    widget.mark_set(tk.INSERT, idx)
        except tk.TclError:
            logger.warning("find_next: TclError")
            return "break"
        if not idx:
            self._set_status(f"«{needle}» не найдено")
            return "break"
        try:
            widget.tag_remove(self.FIND_TAG, "1.0", tk.END)
            widget.tag_add(self.FIND_TAG, idx,
                           f"{idx} + {len(needle)} chars")
            widget.see(idx)
        except tk.TclError:
            return "break"
        self._set_status(f"Найдено: {needle}")
        return "break"

    # ------------------------------------------- переходы к различиям --
    def _jump_diff(self, widget: tk.Misc, forward: bool) -> str:
        """Alt+↓/Alt+↑: следующее/предыдущее различие в этой панели."""
        gaps = self._diff1 if widget is self.text1 else self._diff2
        if not gaps:
            if self._last_matches is None:
                self._set_status("Сначала выполните сравнение")
            else:
                self._set_status("Различий нет")
            return "break"
        if not isinstance(widget, tk.Text):
            return "break"
        cur = _char_offset(widget, tk.INSERT)
        chosen: Optional[tuple[int, int]] = None
        if forward:
            for start, end in gaps:
                if start > cur:
                    chosen = (start, end)
                    break
            if chosen is None:
                chosen = gaps[0]
        else:
            for start, end in reversed(gaps):
                if end < cur:
                    chosen = (start, end)
                    break
            if chosen is None:
                chosen = gaps[-1]
        start, end = chosen
        try:
            idx = f"1.0 + {start} chars"
            widget.mark_set(tk.INSERT, idx)
            widget.see(idx)
            widget.tag_remove(self.FLASH_TAG, "1.0", tk.END)
            flash_end = min(end, start + self.FLASH_MAX_CHARS)
            widget.tag_add(self.FLASH_TAG, idx, f"1.0 + {flash_end} chars")
            widget.after(600, lambda: self._clear_flash(widget))
        except tk.TclError:
            logger.warning("_jump_diff: TclError")
        return "break"

    def _clear_flash(self, widget: tk.Text) -> None:
        if self._closing:
            return
        try:
            widget.tag_remove(self.FLASH_TAG, "1.0", tk.END)
        except tk.TclError:
            pass

    # ------------------------------------------------- файлы и экспорт --
    def swap_panes(self) -> None:
        """Меняет тексты панелей местами."""
        if self._closing:
            return
        t1 = self.text1.get("1.0", "end-1c")
        t2 = self.text2.get("1.0", "end-1c")
        logger.info("swap_panes: %d <-> %d chars", len(t1), len(t2))
        self._insert_chunked(self.text1, t2, "Тексты поменяны местами",
                             "text1")
        self._insert_chunked(self.text2, t1, "Тексты поменяны местами",
                             "text2")

    def save_pane(self, target: str) -> None:
        """Сохраняет содержимое панели в текстовый файл (UTF-8)."""
        widget = self._pane(target)
        try:
            content = widget.get("1.0", "end-1c")
        except tk.TclError:
            return
        title = ("Сохранить «Текст 1»" if target == "text1"
                 else "Сохранить «Текст 2»")
        path = filedialog.asksaveasfilename(
            title=title, defaultextension=".txt",
            initialdir=self._last_dir or "",
            filetypes=[("Текстовые файлы", "*.txt"),
                       ("Все файлы", "*.*")])
        if not path:
            return
        try:
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            logger.exception("save_pane: %s", path)
            self._set_status(f"Не удалось сохранить: {exc}")
            return
        self._last_dir = str(Path(path).parent)
        logger.info("save_pane: %s -> %s (%d chars)", target, path,
                    len(content))
        self._set_status(f"Сохранён: {path}")

    def export_html(self) -> None:
        """Сохраняет HTML-отчёт по последнему сравнению."""
        if self._last_matches is None:
            self._set_status("Сначала выполните сравнение — нечего "
                             "экспортировать")
            return
        t1 = self.text1.get("1.0", "end-1c")
        t2 = self.text2.get("1.0", "end-1c")
        path = filedialog.asksaveasfilename(
            title="Экспорт отчёта в HTML", defaultextension=".html",
            initialdir=self._last_dir or "",
            filetypes=[("HTML-страница", "*.html"),
                       ("Все файлы", "*.*")])
        if not path:
            return
        self._set_status("Формирование HTML-отчёта…")
        self.update_idletasks()
        try:
            report = build_html_report(t1, t2, self._last_matches)
            Path(path).write_text(report, encoding="utf-8")
        except OSError as exc:
            logger.exception("export_html: %s", path)
            self._set_status(f"Не удалось сохранить отчёт: {exc}")
            return
        self._last_dir = str(Path(path).parent)
        logger.info("export_html: %s (%d chars)", path, len(report))
        self._set_status(f"Отчёт сохранён: {path}")

    # ------------------------------------------------------- настройки --
    def settings_snapshot(self) -> dict[str, object]:
        """Текущее состояние настроек для сохранения между запусками."""
        try:
            font_size = int(self.font.cget("size"))
        except (TypeError, ValueError, tk.TclError):
            font_size = self.DEFAULT_FONT_SIZE
        return {
            "mode": self.mode.get(),
            "case_sensitive": bool(self.case_sensitive.get()),
            "font_size": font_size,
            "sync_scroll": bool(self.sync_scroll.get()),
            "show_diffs": bool(self.show_diffs.get()),
        }

    def load_settings(self, data: dict[str, object]) -> None:
        """Применяет сохранённые настройки; чужие значения игнорируются."""
        mode = data.get("mode")
        if isinstance(mode, str) and mode in ("words", "lines", "chars"):
            self.mode.set(mode)
        case_sensitive = data.get("case_sensitive")
        if isinstance(case_sensitive, bool):
            self.case_sensitive.set(case_sensitive)
        sync_scroll = data.get("sync_scroll")
        if isinstance(sync_scroll, bool):
            self.sync_scroll.set(sync_scroll)
        show_diffs = data.get("show_diffs")
        if isinstance(show_diffs, bool):
            self.show_diffs.set(show_diffs)
        font_size = data.get("font_size")
        if (isinstance(font_size, int)
                and self.ZOOM_MIN <= font_size <= self.ZOOM_MAX):
            try:
                self.font.configure(size=font_size)
            except tk.TclError:
                pass

    def on_toggle_sync_scroll(self) -> None:
        self._set_status("Синхронная прокрутка включена"
                         if self.sync_scroll.get()
                         else "Синхронная прокрутка выключена")

    def on_toggle_show_diffs(self) -> None:
        if self.show_diffs.get():
            self._set_status("Подсветка различий включена — применится при "
                             "следующем сравнении")
        else:
            self._set_status("Подсветка различий выключена")

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
            initialdir=self._last_dir or "",
            filetypes=[("Текстовые файлы", "*.txt"),
                       ("Все файлы", "*.*")])
        if not path:
            return
        self._last_dir = str(Path(path).parent)

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
        """Очищает обе панели; старые результаты сравнения сбрасываются."""
        self._suppress_modified += 1
        try:
            self.text1.delete("1.0", tk.END)
            self.text2.delete("1.0", tk.END)
        except tk.TclError:
            logger.warning("clear: TclError, widget already destroyed")
        finally:
            self._suppress_modified -= 1
        self._invalidate_after_edit()
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

        # Новое сравнение гасит незавершённую порционную подсветку.
        self._highlight_gen += 1
        self._highlight_running = False
        self._last_matches = None
        if self._highlight_active:
            self._clear_highlight()

        # --- Тривиальные ветки без воркера ---
        if not t1 and not t2:
            logger.info("compare: both texts empty")
            self._start_highlight([], 0, 0, None, None)
            return
        if not t1 or not t2:
            logger.info("compare: one side empty")
            self._start_highlight([], len(t1), len(t2),
                                  TextDiffer.build_tk_map(t1),
                                  TextDiffer.build_tk_map(t2))
            return

        case_sensitive = self.case_sensitive.get()
        mode = self.mode.get()
        epoch = self._content_epoch
        total_chars = len(t1) + len(t2)

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
                    CompareOk(run_id, matches, len(t1), len(t2),
                              map1, map2, epoch)
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

        # Предупреждения о медленных сценариях ставим после «Сравнение...»:
        # иначе их сразу перезапишет статус по умолчанию.
        if total_chars > self.HUGE_TEXT_CHARS:
            logger.warning("compare: huge texts, %d chars total", total_chars)
            self._set_status(
                f"Сравнение больших текстов "
                f"(~{total_chars // 1000} тыс. символов), подождите…"
            )
        elif mode == "chars" and total_chars > self.CHARS_WARN_CHARS:
            logger.info("compare: chars mode on %d chars total", total_chars)
            self._set_status(
                f"Посимвольное сравнение (~{total_chars // 1000} тыс. "
                f"символов) может занять минуты…"
            )

    def cancel(self) -> None:
        """Прерывает сравнение и подсветку, возвращая UI в покой.

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

        self._highlight_gen += 1
        self._highlight_running = False
        self._last_matches = None
        if self._highlight_active:
            self._clear_highlight()

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
    def _start_highlight(self,
                         matches: list[Match],
                         len1: int,
                         len2: int,
                         map1: TkMap,
                         map2: TkMap) -> None:
        """Подсвечивает совпадения и различия порциями по after-тикам.

        Одиночный tag_add с десятками тысяч диапазонов может заморозить UI
        на минуты, если текст состоит из очень длинных строк (minified JS
        и т.п.): вставка каждого диапазона в B-tree строки стоит тем дороже,
        чем больше диапазонов уже на ней. Поэтому диапазоны применяются
        порциями; размер порции адаптируется под бюджет времени, прогресс
        виден в статусе, «Отмена» прерывает.
        """
        if self._closing:
            return

        self._highlight_gen += 1
        gen = self._highlight_gen
        self._last_matches = matches

        tk_len1 = map1[-1] if map1 is not None else len1
        tk_len2 = map2[-1] if map2 is not None else len2

        blocks: list[tuple[int, int, int, int]] = []
        for s1, e1, s2, e2 in matches:
            if map1 is not None:
                s1, e1 = map1[s1], map1[e1]
            if map2 is not None:
                s2, e2 = map2[s2], map2[e2]
            blocks.append((s1, e1, s2, e2))

        spans1 = [(s1, e1) for s1, e1, _, _ in blocks]
        spans2 = [(s2, e2) for _, _, s2, e2 in blocks]
        # Зазоры между блоками совпадений — это и есть различия.
        self._diff1 = _complement_ranges(spans1, tk_len1)
        self._diff2 = _complement_ranges(spans2, tk_len2)

        matched1 = sum(e1 - s1 for s1, e1, _, _ in matches)
        matched2 = sum(e2 - s2 for _, _, s2, e2 in matches)

        if not blocks:
            # Совпадений нет: совпадений-тегов не будет, но текст, у
            # которого нет пары, целиком является различием.
            if self.show_diffs.get():
                for widget, end in ((self.text1, tk_len1),
                                    (self.text2, tk_len2)):
                    if end <= 0:
                        continue
                    try:
                        widget.tag_add(self.DIFF_TAG, "1.0",
                                       f"1.0 + {end} chars")
                    except tk.TclError:
                        logger.warning("_start_highlight: TclError")
                        return
                    self._highlight_active = True
            self._finish_work(self._stats_text(matched1, matched2,
                                               len1, len2))
            return

        total = len(blocks)
        show_diffs = bool(self.show_diffs.get())
        match_idx1 = [f"1.0 + {v} chars" for s, e in spans1 for v in (s, e)]
        match_idx2 = [f"1.0 + {v} chars" for s, e in spans2 for v in (s, e)]
        diff_idx1 = ([f"1.0 + {v} chars"
                      for s, e in self._diff1 for v in (s, e)]
                     if show_diffs else [])
        diff_idx2 = ([f"1.0 + {v} chars"
                      for s, e in self._diff2 for v in (s, e)]
                     if show_diffs else [])
        gaps1 = len(diff_idx1) // 2
        gaps2 = len(diff_idx2) // 2

        state = {"i": 0, "chunk": self.HIGHLIGHT_START_CHUNK,
                 "d1": 0, "d2": 0}

        try:
            self.compare_btn.config(state=tk.DISABLED)
            self.cancel_btn.config(state=tk.NORMAL)
        except tk.TclError:
            return
        self._highlight_active = True
        self._highlight_running = True

        def step() -> None:
            if self._closing or gen != self._highlight_gen:
                return
            started = time.monotonic()
            i = state["i"]
            size = state["chunk"]
            j = min(i + size, total)
            try:
                if i < j:
                    self.text1.tag_add(self.MATCH_TAG,
                                       *match_idx1[2 * i:2 * j])
                    self.text2.tag_add(self.MATCH_TAG,
                                       *match_idx2[2 * i:2 * j])
                if show_diffs:
                    d1 = min(state["d1"] + size, gaps1)
                    d2 = min(state["d2"] + size, gaps2)
                    if state["d1"] < d1:
                        self.text1.tag_add(
                            self.DIFF_TAG,
                            *diff_idx1[2 * state["d1"]:2 * d1])
                    if state["d2"] < d2:
                        self.text2.tag_add(
                            self.DIFF_TAG,
                            *diff_idx2[2 * state["d2"]:2 * d2])
                    state["d1"], state["d2"] = d1, d2
            except tk.TclError:
                logger.warning("_start_highlight: TclError, stop highlight")
                self._highlight_running = False
                return
            state["i"] = j

            # Адаптация размера порции под бюджет времени.
            elapsed = time.monotonic() - started
            if (elapsed > self.HIGHLIGHT_BUDGET_S * 2
                    and size > self.HIGHLIGHT_MIN_CHUNK):
                state["chunk"] = max(self.HIGHLIGHT_MIN_CHUNK, size // 2)
            elif (elapsed < self.HIGHLIGHT_BUDGET_S / 2
                    and size < self.HIGHLIGHT_MAX_CHUNK):
                state["chunk"] = min(self.HIGHLIGHT_MAX_CHUNK, size * 2)

            done = (j >= total
                    and (not show_diffs
                         or (state["d1"] >= gaps1 and state["d2"] >= gaps2)))
            if done:
                self._highlight_running = False
                self._finish_work(self._stats_text(matched1, matched2,
                                                   len1, len2))
                return

            warn = (" — много блоков, «Отмена» прервёт"
                    if total > self.HIGHLIGHT_WARN_BLOCKS else "")
            self._set_status(f"Подсветка: {j}/{total} блоков…{warn}")
            self.after(1, step)

        self._set_status(f"Подсветка: 0/{total} блоков…")
        self.after(1, step)

    def _stats_text(self, matched1: int, matched2: int,
                    len1: int, len2: int) -> str:
        """Строка статистики сравнения для статус-бара."""
        return (
            f"Текст 1: совпало {matched1}/{len1} симв. "
            f"({_percent(matched1, len1):.1f}%)   |   "
            f"Текст 2: совпало {matched2}/{len2} симв. "
            f"({_percent(matched2, len2):.1f}%)"
        )

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
            if msg.epoch != self._content_epoch:
                logger.info("drain_compare: text changed during run_id=%d",
                            msg.run_id)
                self._finish_work("Текст изменился во время сравнения — "
                                  "сравните заново")
                return
            self._start_highlight(msg.matches, msg.len1, msg.len2,
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
            status = f"Загружен: {msg.path}"
            if msg.warning:
                status += f" — {msg.warning}"
            self._insert_chunked(widget, msg.content, status, msg.target)
        elif isinstance(msg, LoadError):
            self._set_status(f"Не удалось открыть файл: {msg.exc}")

    # --------------------------------------------------- вставка в tk.Text
    def _insert_chunked(self,
                        widget: tk.Text,
                        content: str,
                        done_status: str,
                        target: str) -> None:
        """Вставляет текст порциями, чтобы не морозить UI на больших файлах.

        Токен генерации — свой на каждый target: загрузка в одну панель
        не должна обрывать незавершённую вставку в другую. Замена
        содержимого делает старые результаты сравнения неактуальными.
        """
        self._insert_gens[target] += 1
        gen = self._insert_gens[target]
        total = len(content)

        # Содержимое меняется программно: результаты сравнения устарели.
        self._content_epoch += 1
        self._last_matches = None
        self._diff1 = []
        self._diff2 = []
        for cancel in self._cancel_events.values():
            cancel.set()
        self._cancel_events.clear()
        self._highlight_gen += 1
        if self._highlight_active:
            self._clear_highlight()
        self._restore_idle_buttons()

        self._suppress_modified += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._suppress_modified -= 1

        try:
            widget.delete("1.0", tk.END)
        except tk.TclError:
            logger.warning("_insert_chunked: widget gone, drop insert")
            release()
            return

        if total <= self.INSERT_CHUNK_CHARS:
            try:
                widget.insert(tk.END, content)
            except tk.TclError:
                logger.warning("_insert_chunked: widget gone, drop insert")
                release()
                return
            release()
            self._set_status(done_status)
            return

        logger.info("_insert_chunked: %s %d chars in chunks of %d",
                    target, total, self.INSERT_CHUNK_CHARS)

        def insert_next(offset: int) -> None:
            if self._closing or gen != self._insert_gens[target]:
                release()
                return

            chunk = content[offset:offset + self.INSERT_CHUNK_CHARS]
            try:
                widget.insert(tk.END, chunk)
            except tk.TclError:
                logger.warning("_insert_chunked: widget gone mid-insert")
                release()
                return

            next_offset = offset + len(chunk)
            if next_offset < total:
                self.after(1, insert_next, next_offset)
            else:
                release()
                self._set_status(done_status)

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


def _build_menu(root: tk.Tk, comparator: TextComparator,
                on_close: Callable[[], None]) -> None:
    """Собирает меню окна; акселераторы работают в любой раскладке."""
    menubar = tk.Menu(root)

    file_menu = tk.Menu(menubar, tearoff=False)
    file_menu.add_command(label="Открыть в «Текст 1»…", accelerator="Ctrl+O",
                          command=lambda: comparator.load_file("text1"))
    file_menu.add_command(label="Открыть в «Текст 2»…",
                          accelerator="Ctrl+Shift+O",
                          command=lambda: comparator.load_file("text2"))
    file_menu.add_separator()
    file_menu.add_command(label="Сохранить «Текст 1»…", accelerator="Ctrl+S",
                          command=lambda: comparator.save_pane("text1"))
    file_menu.add_command(label="Сохранить «Текст 2»…",
                          accelerator="Ctrl+Shift+S",
                          command=lambda: comparator.save_pane("text2"))
    file_menu.add_separator()
    file_menu.add_command(label="Экспорт результата в HTML…",
                          command=comparator.export_html)
    file_menu.add_separator()
    file_menu.add_command(label="Выход", command=on_close)
    menubar.add_cascade(label="Файл", menu=file_menu)

    edit_menu = tk.Menu(menubar, tearoff=False)
    edit_menu.add_command(label="Сравнить",
                          accelerator="F5 / Ctrl+Enter",
                          command=comparator.compare)
    edit_menu.add_command(label="Отмена", accelerator="Esc",
                          command=comparator.cancel)
    edit_menu.add_separator()
    edit_menu.add_command(label="Найти…", accelerator="Ctrl+F",
                          command=comparator.toggle_find)
    edit_menu.add_command(label="Поменять тексты местами",
                          command=comparator.swap_panes)
    edit_menu.add_separator()
    edit_menu.add_command(label="Очистить", command=comparator.clear)
    menubar.add_cascade(label="Правка", menu=edit_menu)

    view_menu = tk.Menu(menubar, tearoff=False)
    view_menu.add_checkbutton(label="Синхронная прокрутка",
                              variable=comparator.sync_scroll,
                              command=comparator.on_toggle_sync_scroll)
    view_menu.add_checkbutton(label="Подсвечивать различия",
                              variable=comparator.show_diffs,
                              command=comparator.on_toggle_show_diffs)
    view_menu.add_separator()
    view_menu.add_command(label="Увеличить шрифт",
                          accelerator="Ctrl+колесо вверх",
                          command=lambda: comparator.zoom(1))
    view_menu.add_command(label="Уменьшить шрифт",
                          accelerator="Ctrl+колесо вниз",
                          command=lambda: comparator.zoom(-1))
    menubar.add_cascade(label="Вид", menu=view_menu)

    root.configure(menu=menubar)


def _run_app() -> None:
    _enable_dpi_awareness()

    root = tk.Tk()
    root.title("Сравнение текстов")

    # Окно не должно вылезать за рабочую область на маленьких экранах.
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()
    width = min(1200, max(760, screen_w - 120))
    height = min(700, max(480, screen_h - 160))
    root.geometry(f"{width}x{height}"
                  f"+{max(0, (screen_w - width) // 2)}"
                  f"+{max(0, (screen_h - height) // 3)}")
    root.minsize(760, 480)

    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")

    comparator = TextComparator(root)
    comparator.load_settings(load_settings())
    comparator.pack(fill=tk.BOTH, expand=True)

    def on_close() -> None:
        try:
            save_settings(comparator.settings_snapshot())
        except Exception:  # noqa: BLE001 — настройки не важнее закрытия
            logger.exception("on_close: не удалось сохранить настройки")
        comparator.shutdown()
        root.destroy()
        logger.info("=== text_compare stopped ===")

    _build_menu(root, comparator, on_close)
    _install_error_hooks(root)

    # Акселераторы меню: F5/Ctrl+Enter — сравнить, Esc — отмена.
    root.bind("<F5>", lambda event: comparator.compare())
    root.bind("<Control-Return>", lambda event: comparator.compare())
    root.bind("<Control-KP_Enter>", lambda event: comparator.compare())
    root.bind("<Escape>", lambda event: comparator.escape_action())
    # Ctrl+O/S/F ловим на уровне окна: работает и в нелатинской раскладке.
    root.bind("<Control-KeyPress>", comparator.on_root_ctrl_key, add="+")

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def main() -> None:
    _setup_logging()
    logger.info("=== text_compare started, log=%s ===", LOG_PATH)
    try:
        _run_app()
    except Exception as exc:  # noqa: BLE001 — последний рубеж
        logger.exception("main: фатальная ошибка")
        _show_fatal_error(
            f"Критическая ошибка: {exc}\n\nПодробности в логе:\n{LOG_PATH}")


if __name__ == "__main__":
    main()