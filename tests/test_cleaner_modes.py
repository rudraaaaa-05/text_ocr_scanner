"""Sentence mode and vocab mode of the Cleaner, end to end (file in, Markdown file out).

The fixture tests/data/user_sample_raw.txt is a real OCR output file from a Japanese textbook page: wrapped
sentences, furigana lines in between, headings, and a couple of English labels.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCR_SCANNER_KEYRING", "off")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import ocr_cleaner as c  # noqa: E402
import ocr_core as core  # noqa: E402
import ocr_nlp as n  # noqa: E402
from test_cleaner import FIXED, TmpCase, cfg, echo_caller, table_rows, with_key  # noqa: E402
from test_nlp import FakeTokenizer  # noqa: E402

SAMPLE = (HERE / "data" / "user_sample_raw.txt").read_text(encoding="utf-8")

EXPECTED_SAMPLE_SENTENCES = [
    "くわしく学ぼう",
    "1. 申し出をする表現",
    "(1）目上の人や親しくない人に申し出をするとき",
    "トラックNo.37",
    "状況：へレナは会社員です。",
    "上司に言われて、来客にお茶を出しに応接室に入りました。",
    "ヘレナ……失礼いたします。",
    "よろしかったらお茶をどうぞ。",
    "来客……ああ、すいません。",
    "会話2 トラックNo.38",
    "状況:リュウハのホームステイ先のお父さんが、取扱説明書を見ながらテレビの録画予約をしています。",
    "お父さんは困っているようです。",
    "リュウハ……お父さん、僕がやりましょうか。",
    "お父さん……そうしてもらってもいい?",
    "機械はどうも苦手なんだよね。",
    "主語·述語·補語",
    "How to use",
    "Kiri",
    "N2",
    "きり",
    "例文",
    "Example sentences",
]


class NoSplitterInstalled(TmpCase):
    """Make the tests independent of whether SudachiPy happens to be installed on this machine."""

    def setUp(self):
        super().setUp()
        n._cache["tokenizer"] = None
        for target in (n, c):                       # c imported the name, so both places must be patched
            p = mock.patch.object(target, "optional_tokenizer", return_value=None)
            p.start()
            self.addCleanup(p.stop)


class SentenceModeTests(NoSplitterInstalled):
    def clean(self, text, **kw):
        kw.setdefault("clock", FIXED)
        src = self.write(text)
        return c.clean_file(src, **kw)

    def test_default_mode_is_sentence(self):
        res = self.clean("こんにちは。\n")
        self.assertEqual(res.mode, "sentence")
        self.assertEqual(table_rows(res.markdown)[0], ["#", "Sentence"])
        self.assertIn("Organised: sentence by sentence", res.markdown)

    def test_one_line_with_three_sentences_becomes_three_rows(self):
        res = self.clean("こないだカフェーにいった。どこでしたか？。覚えていない。\n")
        self.assertEqual([r[1] for r in table_rows(res.markdown)[1:]],
                         ["こないだカフェーにいった。", "どこでしたか？。", "覚えていない。"])
        self.assertEqual((res.entries, res.lines_read), (3, 1))

    def test_all_sentences_of_a_line_share_its_time(self):
        res = self.clean("[2026-11-22 18:00:01] 一つ目。二つ目。\n[2026-11-22 18:00:09] 三つ目。\n")
        rows = table_rows(res.markdown)
        self.assertEqual(rows[0], ["#", "Time", "Sentence"])
        self.assertEqual([(r[1], r[2]) for r in rows[1:]], [("2026-11-22 18:00:01", "一つ目。"),
                                                              ("2026-11-22 18:00:01", "二つ目。"),
                                                              ("2026-11-22 18:00:09", "三つ目。")])

    def test_the_users_real_file(self):
        res = self.clean(SAMPLE, language="Japanese")
        rows = table_rows(res.markdown)
        self.assertEqual([r[1] for r in rows[1:]], EXPECTED_SAMPLE_SENTENCES)
        self.assertEqual((res.lines_read, res.entries), (26, 22))
        self.assertEqual(res.furigana, ["じょう", "おうせつしっ", "とりあつかい"])
        self.assertIn("22 sentences from 26 lines (removed 3 furigana lines)", res.markdown)
        self.assertIn("Left out as furigana", res.markdown)
        self.assertIn("じょう, おうせつしっ, とりあつかい", res.markdown)
        self.assertEqual(res.summary(), "22 sentences from 26 lines (3 furigana lines removed)")
        # the two things the user complained about in the old table
        self.assertNotIn("うぞ。", [r[1] for r in rows[1:]])           # half sentence glued back on
        self.assertFalse(any(r[1] in ("じょう", "おうせつしっ", "とりあつかい") for r in rows[1:]))

    def test_furigana_can_be_kept(self):
        res = self.clean(SAMPLE, ignore_furigana=False)
        texts = [r[1] for r in table_rows(res.markdown)[1:]]
        self.assertIn("じょう", texts)
        self.assertEqual(res.furigana, [])
        self.assertNotIn("Left out as furigana", res.markdown)
        self.assertIn("上司に言われて、来客にお", texts)                    # furigana in between: not glued
        self.assertIn("茶を出しに応接室に入りました。", texts)

    def test_repeated_captures_do_not_glue_or_duplicate(self):
        page = "あ" * 30 + "\n" + "い" * 5 + "。\n"
        res = self.clean(page * 3)
        self.assertEqual([r[1] for r in table_rows(res.markdown)[1:]], ["あ" * 30 + "い" * 5 + "。"])

    def test_the_same_sentence_wrapped_differently_is_listed_once(self):
        res = self.clean("あ" * 30 + "。\n" + "あ" * 20 + "\n" + "あ" * 10 + "。\n")
        self.assertEqual(res.entries, 1)

    def test_lines_mode_is_the_old_behaviour(self):
        res = self.clean("こないだ行った。どこだ。\nあ\n", mode="lines")
        self.assertEqual([r[1] for r in table_rows(res.markdown)[1:]], ["こないだ行った。どこだ。", "あ"])
        self.assertEqual(table_rows(res.markdown)[0], ["#", "Text"])
        self.assertIn("Organised: line by line", res.markdown)

    def test_unknown_mode(self):
        with self.assertRaises(c.CleanerError):
            self.clean("x\n", mode="paragraph")

    def test_sentence_translation_gets_whole_sentences(self):
        caller = echo_caller()
        tr = c.Translator(with_key(cfg()), caller=caller, sleep=lambda s: None)
        res = self.clean(SAMPLE, language="Japanese", translate=True, translator=tr)
        sent = json.loads(caller.calls[0][2].split("\n\n")[-1])
        self.assertIn("上司に言われて、来客にお茶を出しに応接室に入りました。", sent.values())   # not "...にお" / "茶を出し..."
        self.assertEqual(table_rows(res.markdown)[0], ["#", "Sentence", "English"])
        self.assertTrue(all(r[2] == "EN:" + r[1] for r in table_rows(res.markdown)[1:]))


class VocabModeTests(NoSplitterInstalled):
    def clean(self, text, **kw):
        kw.setdefault("clock", FIXED)
        kw.setdefault("mode", "vocab")
        return c.clean_file(self.write(text), **kw)

    def test_japanese_vocab_table(self):
        res = self.clean("こないだカフェーにいった。\nお茶を覚えていない。\n", language="Japanese", tokenizer=FakeTokenizer())
        rows = table_rows(res.markdown)
        self.assertEqual(rows[0], ["#", "Word", "Reading", "Sentence"])
        self.assertEqual([(r[1], r[2], r[3]) for r in rows[1:]],
                         [("こないだ", "", "こないだカフェーにいった。"), ("カフェー", "", "こないだカフェーにいった。"),
                          ("行く", "いく", "こないだカフェーにいった。"), ("お茶", "おちゃ", "お茶を覚えていない。"),
                          ("覚える", "おぼえる", "お茶を覚えていない。"), ("いる", "", "お茶を覚えていない。")])
        self.assertIn("Organised: word by word (vocabulary list)", res.markdown)
        self.assertIn("6 words from 2 sentences", res.markdown)
        self.assertEqual(res.summary(), "6 words from 2 sentences")
        self.assertEqual((res.entries, res.sentences, res.mode), (6, 2, "vocab"))

    def test_vocab_builds_on_sentences_so_wrapped_lines_are_rejoined_first(self):
        text = "上司に言われて、来客にお茶を出しに応接室に入りましたがそのままずっと立って\nいました。\n"
        res = self.clean(text, language="Japanese", tokenizer=FakeTokenizer())
        sentences = {r[len(r) - 1] for r in table_rows(res.markdown)[1:]}
        self.assertEqual(sentences, {"上司に言われて、来客にお茶を出しに応接室に入りましたがそのままずっと立っていました。"})

    def test_vocab_translation_sends_each_word_with_its_sentence(self):
        caller = echo_caller("EN:")
        tr = c.Translator(with_key(cfg()), caller=caller, sleep=lambda s: None)
        res = self.clean("こないだカフェーにいった。\n", language="Japanese", tokenizer=FakeTokenizer(),
                         translate=True, translator=tr)
        self.assertEqual(len(caller.calls), 1)
        prompt = caller.calls[0][2]
        self.assertIn("as it is used in that sentence", prompt)
        items = json.loads(prompt.split("\n\n")[-1])
        self.assertEqual(items["3"], {"word": "行く", "sentence": "こないだカフェーにいった。"})
        rows = table_rows(res.markdown)
        self.assertEqual(rows[0], ["#", "Word", "Reading", "English", "Sentence"])
        self.assertEqual(rows[3][:4], ["3", "行く", "いく", "EN:行く"])
        self.assertIn("Translated with:", res.markdown)

    def test_vocab_translation_failures_are_marked_per_word(self):
        def caller(pdef, pc, key, prompt):
            items = json.loads(prompt.split("\n\n")[-1])
            return json.dumps({k: "T" for k, v in items.items() if v["word"] != "カフェー"})
        tr = c.Translator(with_key(cfg()), caller=caller, sleep=lambda s: None)
        res = self.clean("こないだカフェーにいった。\n", language="Japanese", tokenizer=FakeTokenizer(),
                         translate=True, translator=tr)
        rows = table_rows(res.markdown)
        self.assertEqual([r[3] for r in rows[1:]], ["T", c.NOT_TRANSLATED, "T"])
        self.assertEqual((res.translated_count, res.failed_count), (2, 1))
        self.assertIn("1 of 3 words could not be translated", res.markdown)

    def test_english_text_needs_no_splitter_and_has_no_reading_column(self):
        res = self.clean("The cat saw the dog. The dog ran.\n", language="English")
        rows = table_rows(res.markdown)
        self.assertEqual(rows[0], ["#", "Word", "Sentence"])
        self.assertEqual([r[1] for r in rows[1:]], ["the", "cat", "saw", "dog", "ran"])
        self.assertEqual(rows[1][2], "The cat saw the dog.")
        self.assertEqual(rows[5][2], "The dog ran.")

    def test_japanese_without_the_splitter_saves_nothing_and_says_what_to_install(self):
        with mock.patch.object(n, "tokenizer_problem", return_value="needs SudachiPy: pip install sudachipy"):
            with self.assertRaises(c.CleanerError) as cm:
                self.clean("日本語です。\n", language="Japanese")
        self.assertIn("pip install", str(cm.exception))
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_chinese_is_refused_clearly(self):
        with self.assertRaises(c.CleanerError) as cm:
            self.clean("你好吗\n", language="Chinese (Simplified)")
        self.assertIn("sentence mode", str(cm.exception))

    def test_only_particles_gives_a_clear_error(self):
        with self.assertRaises(c.CleanerError) as cm:
            self.clean("には。\n", language="Japanese", tokenizer=FakeTokenizer())
        self.assertIn("No words were found", str(cm.exception))
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_english_language_vocab_with_translate_is_skipped_with_a_note(self):
        res = self.clean("The cat.\n", language="English", translate=True)
        self.assertFalse(res.translated)
        self.assertIn("no English column was added", res.markdown)

    def test_word_limit_for_translation(self):
        text = " ".join(f"w{i}" for i in range(30)) + ".\n"
        with mock.patch.object(c, "MAX_TRANSLATE_ENTRIES", 10):
            with self.assertRaises(c.CleanerError) as cm:
                self.clean(text, language="German", translate=True, translator=c.Translator(with_key(cfg())))
        self.assertIn("30 words", str(cm.exception))


class RenderTests(NoSplitterInstalled):
    @classmethod
    def setUpClass(cls):
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            raise unittest.SkipTest("markdown-it-py is not installed")
        cls.md = MarkdownIt("commonmark").enable("table")

    def cells(self, markdown):
        from test_cleaner import RenderRoundTripTests
        return RenderRoundTripTests.cells_of(self.md.render(markdown))

    def test_sentence_table_renders_every_row_once(self):
        res = c.clean_file(self.write(SAMPLE), language="Japanese", clock=FIXED)
        rows, inner = self.cells(res.markdown)
        self.assertEqual(inner, [])
        self.assertEqual([r[1] for r in rows[1:]], EXPECTED_SAMPLE_SENTENCES)
        self.assertTrue(all(len(r) == 2 for r in rows))

    def test_vocab_table_renders_with_nasty_text_in_every_column(self):
        text = "This | that <b>bold</b> *star* snake_case.\nSecond & third `tick`.\n"
        caller = echo_caller("T | ")
        tr = c.Translator(with_key(cfg()), caller=caller, sleep=lambda s: None)
        res = c.clean_file(self.write(text), mode="vocab", language="German", translate=True, translator=tr,
                           clock=FIXED)
        rows, inner = self.cells(res.markdown)
        self.assertEqual(inner, [])
        self.assertTrue(all(len(r) == 4 for r in rows), rows)          # # | Word | English | Sentence
        self.assertEqual(rows[0], ["#", "Word", "English", "Sentence"])
        sentences = {r[3] for r in rows[1:]}
        self.assertEqual(sentences, {"This | that <b>bold</b> *star* snake_case.", "Second & third `tick`."})
        self.assertTrue(all(r[2] == "T | " + r[1] for r in rows[1:]))


if __name__ == "__main__":
    unittest.main()
