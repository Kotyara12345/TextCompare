"""Тесты чистой логики и заплатки раскладок (unittest, без GUI).

Запуск из корня репозитория:
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import text_compare as tc  # noqa: E402


class _Event:
    """Минимальная заглушка tk.Event для проверки шорткатов."""

    def __init__(self, keysym: str, keycode: int = 0, state: int = 0,
                 keysym_num: int = 0) -> None:
        self.keysym = keysym
        self.keycode = keycode
        self.state = state
        self.keysym_num = keysym_num


CTRL = 0x0004
SHIFT = 0x0001
CAPS = 0x0002


class ShortcutTest(unittest.TestCase):
    """Матрица Ctrl+X/C/V/A/Z/Y для нелатинских раскладок."""

    def test_win_ru_all_shortcuts(self) -> None:
        # На Windows keycode = VK_*, для букв совпадает с ASCII 'A'..'Z'.
        cases = {
            65: "<<SelectAll>>",
            67: "<<Copy>>",
            86: "<<Paste>>",
            88: "<<Cut>>",
            89: "<<Redo>>",
            90: "<<Undo>>",
        }
        for keycode, virtual in cases.items():
            event = _Event("??", keycode=keycode, state=CTRL)
            self.assertEqual(tc._shortcut_for_event(event), virtual)

    def test_capslock_allowed(self) -> None:
        event = _Event("??", keycode=86, state=CTRL | CAPS)
        self.assertEqual(tc._shortcut_for_event(event), "<<Paste>>")

    def test_shift_rejected(self) -> None:
        event = _Event("??", keycode=86, state=CTRL | SHIFT)
        self.assertIsNone(tc._shortcut_for_event(event))

    def test_altgr_rejected(self) -> None:
        event = _Event("??", keycode=86, state=CTRL | tc._ALT_MASK)
        self.assertIsNone(tc._shortcut_for_event(event))

    def test_latin_layout_left_to_tk(self) -> None:
        # Латиница — ASCII-keysym: перевод не нужен, штатный Tk сам
        # обработает <Control-v>, дублировать нельзя.
        event = _Event("v", keycode=86, state=CTRL, keysym_num=0x76)
        self.assertIsNone(tc._shortcut_for_event(event))

    def test_latin1_symbol_rejected(self) -> None:
        # «©» от AltGr+<c> в немецкой раскладке — ввод символа.
        event = _Event("copyright", keycode=67, state=CTRL,
                       keysym_num=0xA9)
        self.assertIsNone(tc._shortcut_for_event(event))

    def test_state_as_string(self) -> None:
        # Конструированные события допускают state строкой.
        event = _Event("??", keycode=86, state="0x4")  # type: ignore[arg-type]
        self.assertEqual(tc._shortcut_for_event(event), "<<Paste>>")

    def test_no_ctrl(self) -> None:
        event = _Event("??", keycode=86, state=0)
        self.assertIsNone(tc._shortcut_for_event(event))

    def test_win_latin_letter(self) -> None:
        with mock.patch.object(tc.sys, "platform", "win32"):
            self.assertEqual(tc._latin_letter(86, "??"), "v")
            self.assertIsNone(tc._latin_letter(45, "Insert"))

    def test_x11_cyrillic_keysym_table(self) -> None:
        with mock.patch.object(tc.sys, "platform", "linux"), \
                mock.patch.object(tc, "_ALT_MASK", 0x0008):
            event = _Event("cyrillic_em", keycode=55, state=CTRL,
                           keysym_num=0x6D5)
            self.assertEqual(tc._shortcut_for_event(event), "<<Paste>>")
            event = _Event("cyrillic_ya", keycode=52, state=CTRL,
                           keysym_num=0x6DA)
            self.assertEqual(tc._shortcut_for_event(event), "<<Undo>>")

    def test_x11_unknown_keysym(self) -> None:
        with mock.patch.object(tc.sys, "platform", "linux"), \
                mock.patch.object(tc, "_ALT_MASK", 0x0008):
            event = _Event("Hangul_J", keycode=38, state=CTRL,
                           keysym_num=0xFF2A)
            self.assertIsNone(tc._shortcut_for_event(event))


class TokenizeTest(unittest.TestCase):
    def test_words(self) -> None:
        tokens = tc.TextDiffer.tokenize("Привет, мир!", "words")
        self.assertEqual([t[0] for t in tokens],
                         ["Привет", ",", " ", "мир", "!"])

    def test_words_punctuation_run(self) -> None:
        tokens = tc.TextDiffer.tokenize("a--b", "words")
        self.assertEqual([t[0] for t in tokens], ["a", "--", "b"])

    def test_lines_keepends(self) -> None:
        tokens = tc.TextDiffer.tokenize("a\nb\n", "lines")
        self.assertEqual([t[0] for t in tokens], ["a\n", "b\n"])

    def test_lines_without_trailing_newline(self) -> None:
        tokens = tc.TextDiffer.tokenize("x\ny", "lines")
        self.assertEqual([t[0] for t in tokens], ["x\n", "y"])

    def test_empty(self) -> None:
        self.assertEqual(tc.TextDiffer.tokenize("", "words"), [])


class ComputeMatchesTest(unittest.TestCase):
    def _compute(self, t1: str, t2: str, mode: str,
                 case_sensitive: bool = True) -> list[tc.Match]:
        return tc.TextDiffer.compute_matches(
            t1, t2, mode, case_sensitive, threading.Event())

    def test_identical_words(self) -> None:
        matches = self._compute("привет мир", "привет мир", "words")
        self.assertEqual(matches, [(0, 10, 0, 10)])

    def test_words_case_insensitive(self) -> None:
        matches = self._compute("Привет мир", "привет мир", "words",
                                case_sensitive=False)
        self.assertEqual(matches, [(0, 10, 0, 10)])

    def test_chars_min_match_filters_short_blocks(self) -> None:
        # Общие куски «abc» (3) и «ef» (2) короче min_match=4.
        self.assertEqual(self._compute("abcdef", "abcxef", "chars"), [])

    def test_chars_case_insensitive(self) -> None:
        matches = self._compute("ABCDEF", "abcdef", "chars",
                                case_sensitive=False)
        self.assertEqual(matches, [(0, 6, 0, 6)])
        self.assertEqual(self._compute("ABCDEF", "abcdef", "chars"), [])

    def test_lines_mode(self) -> None:
        matches = self._compute("x\nY\nz", "x\ny\nz", "lines")
        # Совпадают строки «x\n» и «z», различается «Y\n»/«y\n».
        self.assertEqual(matches, [(0, 2, 0, 2), (4, 5, 4, 5)])

    def test_cancel_before_start(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(tc._Cancelled):
            tc.TextDiffer.compute_matches("a", "b", "words", True, cancel)


class DifflibCostTest(unittest.TestCase):
    """Защита от квадратичной деградации difflib на частых токенах."""

    def test_choose_junk_picks_dominant_token(self) -> None:
        seq = [tok for i in range(2000) for tok in (f"u{i:05d}", "P")]
        junk, before, after = tc.TextDiffer._choose_junk(seq, seq)
        self.assertEqual(junk, {"P"})
        self.assertGreater(before, tc.TextDiffer.MATCH_COST_LIMIT)
        self.assertLessEqual(after, tc.TextDiffer.MATCH_COST_LIMIT)

    def test_choose_junk_small_sequences(self) -> None:
        junk, before, after = tc.TextDiffer._choose_junk(
            ["a", " ", "b"], ["a", " ", "c"])
        self.assertEqual(junk, set())
        self.assertEqual(before, after)

    def test_identical_big_text_single_span(self) -> None:
        text = " ".join(f"w{i:05d}" for i in range(4000))
        matches = tc.TextDiffer.compute_matches(
            text, text, "words", False, threading.Event())
        self.assertEqual(matches, [(0, len(text), 0, len(text))])

    def test_whitespace_only_diff_is_gap(self) -> None:
        matches = tc.TextDiffer.compute_matches(
            "x y", "x  y", "words", True, threading.Event())
        self.assertEqual(matches, [(0, 1, 0, 1), (2, 3, 3, 4)])

    def test_pathological_alternating_words(self) -> None:
        # Каждое второе слово различается: со старым autojunk=False
        # difflib вешался на таких данных на минуты.
        n = 1500
        words1 = [f"w{i:05d}" for i in range(n)]
        words2 = list(words1)
        for i in range(0, n, 2):
            words2[i] = f"X{i:05d}"
        t1 = " ".join(words1)
        t2 = " ".join(words2)
        started = time.perf_counter()
        matches = tc.TextDiffer.compute_matches(
            t1, t2, "words", True, threading.Event())
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 15.0, "difflib снова деградировал")
        self.assertEqual(len(matches), n // 2)
        for s1, e1, s2, e2 in matches:
            self.assertEqual(t1[s1:e1], t2[s2:e2])

    def test_blank_lines_junked_but_edges_kept(self) -> None:
        t1 = "\n".join(
            (f"line{i:05d}" if i % 2 else "") for i in range(2000))
        t2 = t1.replace("line00077", "CHANGED")
        matches = tc.TextDiffer.compute_matches(
            t1, t2, "lines", True, threading.Event())
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0][0], 0)
        self.assertEqual(matches[1][1], len(t1))
        for s1, e1, s2, e2 in matches:
            self.assertEqual(t1[s1:e1], t2[s2:e2])


class BuildTkMapTest(unittest.TestCase):
    def test_ascii_is_none(self) -> None:
        self.assertIsNone(tc.TextDiffer.build_tk_map("hello"))

    def test_bmp_non_ascii_is_none(self) -> None:
        self.assertIsNone(tc.TextDiffer.build_tk_map("привет"))

    def test_astral_counts_double(self) -> None:
        # Эмодзи в UTF-16 занимает два code unit.
        self.assertEqual(tc.TextDiffer.build_tk_map("a😀b"), [0, 1, 3, 4])


class ComplementRangesTest(unittest.TestCase):
    def test_empty_spans(self) -> None:
        self.assertEqual(tc._complement_ranges([], 10), [(0, 10)])

    def test_middle_gaps(self) -> None:
        self.assertEqual(tc._complement_ranges([(2, 4), (6, 8)], 10),
                         [(0, 2), (4, 6), (8, 10)])

    def test_covering_spans(self) -> None:
        self.assertEqual(tc._complement_ranges([(0, 3)], 3), [])

    def test_zero_total(self) -> None:
        self.assertEqual(tc._complement_ranges([], 0), [])


class PercentTest(unittest.TestCase):
    def test_half(self) -> None:
        self.assertEqual(tc._percent(5, 10), 50.0)

    def test_zero_total(self) -> None:
        self.assertEqual(tc._percent(1, 0), 0.0)


class HtmlReportTest(unittest.TestCase):
    def test_escapes_and_marks(self) -> None:
        report = tc.build_html_report("<a>x", "<b>y", [(3, 4, 3, 4)])
        self.assertIn("&lt;a&gt;", report)
        self.assertIn("&lt;b&gt;", report)
        self.assertIn('<span class="match">x</span>', report)

    def test_no_matches(self) -> None:
        report = tc.build_html_report("a", "b", [])
        self.assertNotIn('class="match"', report)
        self.assertIn("<h2>Текст 1</h2>", report)


if __name__ == "__main__":
    unittest.main()
