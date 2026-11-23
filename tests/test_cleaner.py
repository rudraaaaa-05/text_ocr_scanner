"""Tests for the Cleaner (ocr_cleaner.py). Qt-free, no network: translation is tested with fake providers and a
mocked requests.post.

Run with:  python -m unittest discover -s tests -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

os.environ.setdefault("OCR_SCANNER_KEYRING", "off")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ocr_cleaner as c  # noqa: E402
import ocr_core as core  # noqa: E402

FIXED = lambda: datetime(2026, 11, 21, 14, 5)  # noqa: E731


def cfg(**over):
    d = core.default_config()
    d.update(over)
    return d


def with_key(d, pid="gemini", keys="k1"):
    d["providers"][pid]["keys"] = keys
    return d


def split_row(line: str) -> list:
    """Split a table row on UNESCAPED pipes only, the way a Markdown renderer does."""
    body = line.strip()
    assert body.startswith("|") and body.endswith("|"), line
    cells = re.split(r"(?<!\\)\|", body)[1:-1]
    return [x.strip() for x in cells]


def table_rows(md: str) -> list:
    """All rows of the (single) table in the document, header first, separator removed."""
    lines = [ln for ln in md.splitlines() if ln.startswith("|")]
    rows = [split_row(ln) for ln in lines]
    assert len(rows) >= 2
    assert all(re.fullmatch(r":?-{2,}:?", x) for x in rows[1]), "second row must be the separator"
    return [rows[0]] + rows[2:]


def echo_caller(prefix="EN:"):
    """A fake provider that 'translates' by prefixing each entry; records every prompt it receives."""
    calls = []

    def caller(pdef, pc, key, prompt):
        calls.append((pdef["id"], key, prompt))
        items = json.loads(prompt.split("\n\n")[-1])
        return json.dumps({k: prefix + (v["word"] if isinstance(v, dict) else v) for k, v in items.items()},
                          ensure_ascii=False)

    caller.calls = calls
    return caller


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, text, name="ocr_output_2026-11-21_10-00-00.txt", raw=None):
        p = self.dir / name
        if raw is not None:
            p.write_bytes(raw)
        else:
            p.write_text(text, encoding="utf-8", newline="")
        return p


# ---------------------------------------------------------------------------------------------------------------
class ParsingTests(unittest.TestCase):
    def test_timestamps_are_stripped_and_kept_as_time(self):
        p = c.parse_output("[2026-01-31 14:05:09] hello world\n[2026-01-31 14:05:12] second line\n")
        self.assertEqual([e.text for e in p.entries], ["hello world", "second line"])
        self.assertEqual(p.entries[0].first_seen, "2026-01-31 14:05:09")
        self.assertTrue(p.has_times)

    def test_plain_file_has_no_time_column(self):
        p = c.parse_output("alpha\nbeta\n")
        self.assertFalse(p.has_times)
        self.assertIsNone(p.entries[0].first_seen)

    def test_bracketed_text_that_is_not_a_timestamp_is_kept(self):
        p = c.parse_output("[NOTE] keep me\n[2026-1-1] short date\n")
        self.assertEqual([e.text for e in p.entries], ["[NOTE] keep me", "[2026-1-1] short date"])

    def test_blank_and_noise_lines(self):
        p = c.parse_output("\n   \n|\n---\n....\n\u200b\n[2026-01-31 14:05:09] ~~\nreal text\n")
        self.assertEqual([e.text for e in p.entries], ["real text"])
        self.assertEqual(p.noise, 5)           # | --- .... zero-width, stamped ~~
        self.assertEqual(p.lines_read, 6)      # blank lines are not counted

    def test_single_letter_and_digit_lines_are_kept(self):
        p = c.parse_output("I\n7\n猫\n")
        self.assertEqual([e.text for e in p.entries], ["I", "7", "猫"])

    def test_duplicates_ignore_case_width_spacing_and_punctuation(self):
        p = c.parse_output("Hello, World!\nhello world\nHELLO   WORLD.\n\uff28\uff45\uff4c\uff4c\uff4f \uff37orld\nother\n")
        self.assertEqual([e.text for e in p.entries], ["Hello, World!", "other"])
        self.assertEqual(p.duplicates, 3)

    def test_first_occurrence_order_is_kept(self):
        p = c.parse_output("c\nb\nc\na\nb\n")
        self.assertEqual([e.text for e in p.entries], ["c", "b", "a"])

    def test_different_text_is_not_merged(self):
        p = c.parse_output("cat\ncats\ncat2\n")
        self.assertEqual(len(p.entries), 3)

    def test_japanese_gaps_between_characters_are_removed(self):
        self.assertEqual(c.normalize_text("今日は\u3000天気が  いいですね"), "今日は天気がいいですね")
        self.assertEqual(c.normalize_text("iPhone を 買う"), "iPhone を買う")   # Latin side keeps its space
        p = c.parse_output("今日は 天気\n今日は天気。\n")
        self.assertEqual(len(p.entries), 1)

    def test_invisible_characters_and_tabs(self):
        self.assertEqual(c.normalize_text("a\u200bb\ufeffc\tdd\x07 e"), "abc dd e")

    def test_crlf_and_old_mac_line_endings(self):
        p = c.parse_output("one\r\ntwo\rthree\n")
        self.assertEqual([e.text for e in p.entries], ["one", "two", "three"])


class TableTests(unittest.TestCase):
    def build(self, texts, **kw):
        parsed = c.parse_output("\n".join(texts))
        kw.setdefault("with_times", False)
        return c.build_markdown(parsed.entries, source_name="x.txt", language="Auto-detect", translated=False,
                                parsed=parsed, clock=FIXED, **kw)

    def test_structure(self):
        md = self.build(["alpha", "beta"])
        self.assertTrue(md.startswith("# OCR table\n"))
        rows = table_rows(md)
        self.assertEqual(rows[0], ["#", "Text"])
        self.assertEqual(rows[1:], [["1", "alpha"], ["2", "beta"]])
        self.assertTrue(md.endswith("\n") and not md.endswith("\n\n"))
        self.assertNotIn("\r", md)

    def test_pipe_in_text_cannot_break_the_table(self):
        md = self.build(["a | b", "x||y", "trailing |", "back\\slash"])
        rows = table_rows(md)
        self.assertTrue(all(len(r) == 2 for r in rows), rows)
        self.assertEqual(rows[1][1], "a \\| b")
        self.assertEqual(rows[3][1], "trailing \\|")

    def test_every_row_has_the_same_number_of_cells(self):
        md = self.build(["猫 | 犬", "plain", "a|b|c|d", "日本語のとても長い文章" * 10])
        widths = {len(r) for r in table_rows(md)}
        self.assertEqual(widths, {2})

    def test_columns_line_up_for_wide_characters(self):
        md = self.build(["猫が好きです", "hello", "犬"])
        lines = [ln for ln in md.splitlines() if ln.startswith("|")]
        self.assertEqual(len({c.display_width(ln) for ln in lines}), 1, lines)

    def test_very_long_cell_is_not_padded_into_every_row(self):
        md = self.build(["short", "x" * 300])
        short_row = [ln for ln in md.splitlines() if "short" in ln][0]
        self.assertLess(len(short_row), c.PAD_CAP + 20)

    def test_time_and_english_columns(self):
        parsed = c.parse_output("[2026-11-21 10:00:00] hi\n")
        parsed.entries[0].translation = "hello | there"
        md = c.build_markdown(parsed.entries, source_name="x.txt", language="Japanese", translated=True,
                              with_times=True, parsed=parsed, provider_name="Groq", clock=FIXED)
        rows = table_rows(md)
        self.assertEqual(rows[0], ["#", "Time", "Text", "English"])
        self.assertEqual(rows[1], ["1", "2026-11-21 10:00:00", "hi", "hello \\| there"])
        self.assertIn("Translated with: Groq", md)
        self.assertIn("Language: Japanese", md)

    def test_untranslated_row_is_marked_not_blank(self):
        parsed = c.parse_output("a\nb\n")
        parsed.entries[0].translation = "A"
        md = c.build_markdown(parsed.entries, source_name="x", language="", translated=True, with_times=False,
                              parsed=parsed, clock=FIXED)
        self.assertEqual(table_rows(md)[2][2], c.NOT_TRANSLATED)

    def test_header_stats(self):
        parsed = c.parse_output("a\na\n|\nb\n")
        md = c.build_markdown(parsed.entries, source_name="x", language="Auto-detect", translated=False,
                              with_times=False, parsed=parsed, clock=FIXED)
        self.assertIn("2 entries from 4 lines (removed 1 duplicate and 1 noise line)", md)
        self.assertIn("Created: 2026-11-21 14:05", md)


# ---------------------------------------------------------------------------------------------------------------
class CleanFileTests(TmpCase):
    def test_writes_table_next_to_source_and_leaves_source_alone(self):
        src = self.write("[2026-11-21 10:00:03] hello\n[2026-11-21 10:00:06] hello\n[2026-11-21 10:00:09] world\n")
        before = src.read_bytes()
        res = c.clean_file(src, clock=FIXED)
        self.assertEqual(res.path, self.dir / "ocr_output_2026-11-21_10-00-00_table.md")
        self.assertEqual(src.read_bytes(), before)
        text = res.path.read_text(encoding="utf-8")
        self.assertEqual(text, res.markdown)
        self.assertEqual(table_rows(text)[1:], [["1", "2026-11-21 10:00:03", "hello"], ["2", "2026-11-21 10:00:09", "world"]])
        self.assertEqual((res.entries, res.duplicates, res.lines_read), (2, 1, 3))

    def test_output_is_utf8_without_bom_and_lf_only(self):
        src = self.write("猫が好きです\r\n犬\r\n")
        raw = c.clean_file(src, clock=FIXED).path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", raw)
        self.assertIn("猫が好きです".encode("utf-8"), raw)

    def test_source_with_bom_and_invalid_bytes(self):
        src = self.write("", raw=b"\xef\xbb\xbfhello\nbad \xff byte\n")
        res = c.clean_file(src, clock=FIXED)
        texts = [r[1] for r in table_rows(res.markdown)[1:]]
        self.assertEqual(texts[0], "hello")          # BOM did not end up in the first cell
        self.assertEqual(len(texts), 2)

    def test_never_overwrites_an_existing_table(self):
        src = self.write("one\ntwo\n")
        first = c.clean_file(src, clock=FIXED).path
        first_bytes = first.read_bytes()
        second = c.clean_file(src, clock=FIXED).path
        third = c.clean_file(src, clock=FIXED).path
        self.assertEqual([p.name for p in (first, second, third)],
                         [src.stem + "_table.md", src.stem + "_table_2.md", src.stem + "_table_3.md"])
        self.assertEqual(first.read_bytes(), first_bytes)

    def test_out_dir(self):
        src = self.write("one\n")
        res = c.clean_file(src, out_dir=self.dir / "new" / "folder", clock=FIXED)
        self.assertEqual(res.path.parent, self.dir / "new" / "folder")
        self.assertTrue(res.path.exists())

    def test_errors_write_nothing(self):
        for name, body, msg in (("empty.txt", "", "empty"), ("blank.txt", "\n  \n\n", "empty"),
                                ("noise.txt", "|\n---\n...\n", "no readable text")):
            p = self.write(body, name=name)
            with self.assertRaises(c.CleanerError) as cm:
                c.clean_file(p)
            self.assertIn(msg, str(cm.exception))
        with self.assertRaises(c.CleanerError):
            c.clean_file(self.dir / "missing.txt")
        with self.assertRaises(c.CleanerError):
            c.clean_file(self.dir)                          # a folder is not a file
        self.assertEqual(sorted(p.name for p in self.dir.glob("*_table*")), [])

    def test_huge_file_is_refused(self):
        p = self.write("x\n")
        with mock.patch.object(c, "MAX_SOURCE_BYTES", 1):
            with self.assertRaises(c.CleanerError):
                c.clean_file(p)

    def test_unwritable_target_is_a_readable_error(self):
        src = self.write("one\n")
        blocker = self.dir / "blocker"
        blocker.write_text("i am a file, not a folder")
        with self.assertRaises(c.CleanerError) as cm:
            c.clean_file(src, out_dir=blocker / "sub")
        self.assertIn("Cannot create the folder", str(cm.exception))

    def test_real_world_sample_end_to_end(self):
        src = self.write(
            "[2026-11-21 10:00:03] 猫が好きです\n[2026-11-21 10:00:03] |\n[2026-11-21 10:00:06] 猫が好きです。\n"
            "[2026-11-21 10:00:06] 今日は　天気が  いいですね\n[2026-11-21 10:00:09] ---\n"
            "[2026-11-21 10:00:09] 犬 | 猫\n[2026-11-21 10:00:12] 今日は天気がいいですね\n\n"
            "[2026-11-21 10:00:15] ＡＢＣ  Hello\n[2026-11-21 10:00:15] abc hello!\n")
        res = c.clean_file(src, language="Japanese", clock=FIXED)
        rows = table_rows(res.markdown)
        self.assertEqual([r[2] for r in rows[1:]], ["猫が好きです", "今日は天気がいいですね", "犬 \\| 猫", "ＡＢＣ Hello"])
        self.assertEqual((res.entries, res.duplicates, res.noise), (4, 3, 2))


# ---------------------------------------------------------------------------------------------------------------
NASTY = [
    "a | b || c", "x|y", "a\\|b", "a\\\\|b", "trailing |", "| leading", "C:\\Users\\me\\file", "ends with backslash\\",
    "back\\*slash star", "Press <Enter> or <Esc>", "<b>bold</b> & <script>x</script>", "*star* _under_ `tick`",
    "[link](http://x) ![img](y)", "snake_case_name and __dunder__", "~~strike~~", "AT&T &amp; &copy; &#39; &lt;",
    "1. not a list", "- not a bullet", "# not a heading", "> not a quote", "---", "***", "$100 and $200",
    "猫が好きです", "犬 | 猫 | 鳥", "ＡＢＣ Hello", "héllo wörld ñ 😀", "a" * 300, "see http://example.com/a_b_c?x=1&y=2",
]


class RenderRoundTripTests(unittest.TestCase):
    """The strongest check that the files are clean: a real Markdown engine renders every cell back to exactly
    the text that was on screen, with no formatting, links, tags or broken rows."""

    @classmethod
    def setUpClass(cls):
        try:
            from markdown_it import MarkdownIt
        except ImportError:
            raise unittest.SkipTest("markdown-it-py is not installed")
        cls.md = MarkdownIt("commonmark").enable("table")

    @staticmethod
    def cells_of(html_text):
        from html.parser import HTMLParser

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows, self.row, self.buf, self.in_cell, self.inner_tags = [], None, "", False, []

            def handle_starttag(self, tag, attrs):
                if tag == "tr":
                    self.row = []
                elif tag in ("td", "th"):
                    self.in_cell, self.buf = True, ""
                elif self.in_cell:
                    self.inner_tags.append(tag)

            def handle_endtag(self, tag):
                if tag in ("td", "th"):
                    self.row.append(self.buf)
                    self.in_cell = False
                elif tag == "tr":
                    self.rows.append(self.row)

            def handle_data(self, data):
                if self.in_cell:
                    self.buf += data
        p = P()
        p.feed(html_text)
        return p.rows, p.inner_tags

    def render(self, texts, translated=False):
        parsed = c.parse_output("\n".join(texts))
        for e in parsed.entries:
            if translated:
                e.translation = "T:" + e.text
        md = c.build_markdown(parsed.entries, source_name="x.txt", language="Auto-detect", translated=translated,
                              with_times=False, parsed=parsed, clock=FIXED)
        return parsed, md, self.md.render(md)

    def test_every_nasty_string_survives_a_real_renderer(self):
        parsed, md, html_out = self.render(NASTY)
        rows, inner = self.cells_of(html_out)
        self.assertEqual(inner, [], f"formatting leaked into cells: {inner}")
        self.assertEqual(len(rows) - 1, len(parsed.entries))
        for row, e in zip(rows[1:], parsed.entries):
            self.assertEqual(len(row), 2, row)
            self.assertEqual(row[1], e.text)

    def test_translation_column_round_trips_too(self):
        parsed, md, html_out = self.render(NASTY, translated=True)
        rows, inner = self.cells_of(html_out)
        self.assertEqual(inner, [])
        for row, e in zip(rows[1:], parsed.entries):
            self.assertEqual((len(row), row[1], row[2]), (3, e.text, "T:" + e.text))

    def test_header_block_does_not_swallow_the_table(self):
        _, md, html_out = self.render(["a", "b"])
        self.assertEqual(html_out.count("<table>"), 1)
        rows, _ = self.cells_of(html_out)
        self.assertEqual([r[1] for r in rows], ["Text", "a", "b"])


class ReplyParsingTests(unittest.TestCase):
    IDS = ["1", "2", "3"]

    def test_plain_json(self):
        self.assertEqual(c.parse_translation_reply('{"1": "a", "2": "b", "3": "c"}', self.IDS),
                         {"1": "a", "2": "b", "3": "c"})

    def test_code_fence_and_chatter(self):
        r = c.parse_translation_reply('```json\n{"1": "a", "2": "b"}\n```', self.IDS)
        self.assertEqual(r, {"1": "a", "2": "b"})
        r = c.parse_translation_reply('Sure! Here you go:\n{"1": "a"}\nHope that helps', self.IDS)
        self.assertEqual(r, {"1": "a"})

    def test_missing_empty_and_unknown_ids_are_not_invented(self):
        r = c.parse_translation_reply('{"1": "a", "2": "  ", "9": "zzz", "3": null}', self.IDS)
        self.assertEqual(r, {"1": "a"})

    def test_numbers_become_text_and_newlines_collapse(self):
        r = c.parse_translation_reply('{"1": 42, "2": "two\\nlines"}', self.IDS)
        self.assertEqual(r, {"1": "42", "2": "two lines"})

    def test_array_reply_only_accepted_when_length_matches(self):
        self.assertEqual(c.parse_translation_reply('["a", "b", "c"]', self.IDS), {"1": "a", "2": "b", "3": "c"})
        self.assertEqual(c.parse_translation_reply('["a", "b"]', self.IDS), {})

    def test_numbered_lines_fallback(self):
        r = c.parse_translation_reply("1: cat\n2. dog\n3) bird", self.IDS)
        self.assertEqual(r, {"1": "cat", "2": "dog", "3": "bird"})

    def test_garbage(self):
        self.assertEqual(c.parse_translation_reply("I cannot help with that.", self.IDS), {})
        self.assertEqual(c.parse_translation_reply("", self.IDS), {})
        self.assertEqual(c.parse_translation_reply("{not json", self.IDS), {})


class TranslatorTests(TmpCase):
    def translator(self, d=None, provider="auto", caller=None, sleep=None):
        d = d or with_key(cfg())
        return c.Translator(d, provider, caller=caller or echo_caller(), sleep=sleep or (lambda s: None))

    def test_prompt_mentions_language_or_autodetect(self):
        self.assertIn("are Japanese", c.build_translation_prompt({"1": "x"}, "Japanese"))
        self.assertIn("automatically detect", c.build_translation_prompt({"1": "x"}, "Auto-detect"))
        self.assertIn("automatically detect", c.build_translation_prompt({"1": "x"}, ""))

    def test_translate_with_english_column_end_to_end(self):
        src = self.write("[2026-11-21 10:00:03] 猫\n[2026-11-21 10:00:06] 犬\n")
        caller = echo_caller()
        res = c.clean_file(src, translate=True, language="Japanese", translator=self.translator(caller=caller),
                           clock=FIXED)
        rows = table_rows(res.markdown)
        self.assertEqual(rows[0], ["#", "Time", "Sentence", "English"])
        self.assertEqual([r[3] for r in rows[1:]], ["EN:猫", "EN:犬"])
        self.assertTrue(res.translated)
        self.assertEqual((res.translated_count, res.failed_count), (2, 0))
        self.assertIn("Translated with: Google AI Studio (Gemini)", res.markdown)
        self.assertEqual(len(caller.calls), 1)
        self.assertIn("are Japanese", caller.calls[0][2])

    def test_duplicates_are_translated_once(self):
        src = self.write("cat\ncat\nCAT.\ndog\n")
        caller = echo_caller()
        c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        items = json.loads(caller.calls[0][2].split("\n\n")[-1])
        self.assertEqual(list(items.values()), ["cat", "dog"])

    def test_translation_with_pipe_does_not_break_the_table(self):
        src = self.write("one\ntwo\n")

        def caller(pdef, pc, key, prompt):
            return '{"1": "a | b", "2": "c"}'
        res = c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        rows = table_rows(res.markdown)
        self.assertTrue(all(len(r) == 3 for r in rows))
        self.assertEqual(rows[1][2], "a \\| b")

    def test_batching_by_item_count(self):
        n = c.Translator.MAX_ITEMS * 2 + 5
        src = self.write("\n".join(f"word{i}" for i in range(n)))
        caller = echo_caller()
        res = c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        self.assertEqual(len(caller.calls), 3)
        self.assertEqual(res.translated_count, n)
        rows = table_rows(res.markdown)[1:]
        self.assertTrue(all(r[2] == "EN:" + r[1] for r in rows))     # every row got ITS OWN translation, in order

    def test_batching_by_characters(self):
        long = "x" * 1000
        batches = list(self.translator()._batches([0, 1, 2, 3], [long + str(i) for i in range(4)]))
        self.assertEqual(batches, [[0, 1], [2, 3]])        # 3000-character budget, 1001 per entry
        one = list(self.translator()._batches([0], ["y" * 10_000]))
        self.assertEqual(one, [[0]])                       # an oversized entry still gets its own batch

    def test_progress_and_final_count(self):
        src = self.write("\n".join(f"w{i}" for i in range(c.Translator.MAX_ITEMS + 1)))
        seen = []
        c.clean_file(src, translate=True, translator=self.translator(), clock=FIXED,
                     progress=lambda d, t, m: seen.append((d, t, m)))
        translating = [s for s in seen if s[2] == "Translating"]
        self.assertEqual(translating[-1][:2], (c.Translator.MAX_ITEMS + 1, c.Translator.MAX_ITEMS + 1))
        self.assertEqual(seen[-1][2], "Done")

    def test_model_skips_entries_then_gets_asked_again_for_just_those(self):
        src = self.write("a1\nb2\nc3\nd4\n")
        asked = []

        def caller(pdef, pc, key, prompt):
            items = json.loads(prompt.split("\n\n")[-1])
            asked.append(list(items.values()))
            keep = {k: "T-" + v for k, v in items.items() if v != "c3" or len(asked) > 1}
            return json.dumps(keep)
        res = c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        self.assertEqual(asked, [["a1", "b2", "c3", "d4"], ["c3"]])
        self.assertEqual([r[2] for r in table_rows(res.markdown)[1:]], ["T-a1", "T-b2", "T-c3", "T-d4"])

    def test_model_that_scrambles_a_batch_is_split_not_trusted(self):
        src = self.write("a1\nb2\nc3\nd4\n")

        def caller(pdef, pc, key, prompt):
            items = json.loads(prompt.split("\n\n")[-1])
            if len(items) > 2:
                return "Sorry, I can only do short lists."
            return json.dumps({k: "T-" + v for k, v in items.items()})
        res = c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        self.assertEqual([r[2] for r in table_rows(res.markdown)[1:]], ["T-a1", "T-b2", "T-c3", "T-d4"])
        self.assertEqual(res.failed_count, 0)

    def test_entry_the_model_never_answers_is_marked_and_reported(self):
        src = self.write("good\nbad\n")

        def caller(pdef, pc, key, prompt):
            items = json.loads(prompt.split("\n\n")[-1])
            return json.dumps({k: "T-" + v for k, v in items.items() if v != "bad"})
        res = c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        rows = table_rows(res.markdown)
        self.assertEqual([r[2] for r in rows[1:]], ["T-good", c.NOT_TRANSLATED])
        self.assertEqual((res.translated_count, res.failed_count), (1, 1))
        self.assertIn("1 of 2 entries could not be translated", res.markdown)
        self.assertIn("could not be translated", res.summary())

    def test_total_failure_saves_nothing(self):
        src = self.write("one\ntwo\n")

        def caller(pdef, pc, key, prompt):
            raise core.OCRError("HTTP 401: API key not valid")
        with self.assertRaises(c.CleanerError) as cm:
            c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        self.assertIn("nothing was saved", str(cm.exception))
        self.assertIn("API key not valid", str(cm.exception))
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_provider_that_keeps_failing_is_not_hammered(self):
        n = c.Translator.MAX_ITEMS * 5
        src = self.write("\n".join(f"w{i}" for i in range(n)))
        calls = []

        def caller(pdef, pc, key, prompt):
            calls.append(1)
            items = json.loads(prompt.split("\n\n")[-1])
            if len(calls) <= 1:
                return json.dumps({k: "T-" + v for k, v in items.items()})
            raise core.OCRError("HTTP 500: boom")
        res = c.clean_file(src, translate=True, translator=self.translator(caller=caller), clock=FIXED)
        self.assertEqual(len(calls), 1 + c.Translator.MAX_FAILED_BATCHES_IN_A_ROW)
        self.assertEqual(res.translated_count, c.Translator.MAX_ITEMS)
        self.assertEqual(res.translated_count + res.failed_count, n)
        self.assertEqual(sum(1 for r in table_rows(res.markdown)[1:] if r[2] == c.NOT_TRANSLATED), n - c.Translator.MAX_ITEMS)

    def test_rate_limit_waits_then_succeeds(self):
        src = self.write("one\n")
        attempts, slept = [], []

        def caller(pdef, pc, key, prompt):
            attempts.append(key)
            if len(attempts) < 3:
                raise core.OCRError("HTTP 429: quota")
            return '{"1": "uno"}'
        res = c.clean_file(src, translate=True, clock=FIXED,
                           translator=self.translator(caller=caller, sleep=slept.append))
        self.assertEqual(slept, [3.0, 8.0])
        self.assertEqual(table_rows(res.markdown)[1][2], "uno")

    def test_rate_limit_that_never_clears_gives_up(self):
        src = self.write("one\n")
        slept = []

        def caller(pdef, pc, key, prompt):
            raise core.OCRError("HTTP 429: quota")
        with self.assertRaises(c.CleanerError) as cm:
            c.clean_file(src, translate=True, translator=self.translator(caller=caller, sleep=slept.append))
        self.assertIn("429", str(cm.exception))
        self.assertEqual(len(slept), 2)

    def test_keys_rotate_and_bad_key_falls_through_to_next(self):
        d = with_key(cfg(), keys="bad, good")
        used = []

        def caller(pdef, pc, key, prompt):
            used.append(key)
            if key == "bad":
                raise core.OCRError("HTTP 401: nope")
            return '{"1": "ok"}'
        src = self.write("one\n")
        c.clean_file(src, translate=True, translator=self.translator(d, caller=caller), clock=FIXED)
        self.assertEqual(used, ["bad", "good"])
        src2 = self.write("two\n", name="b.txt")
        used.clear()
        c.clean_file(src2, translate=True, translator=self.translator(d, caller=caller), clock=FIXED)
        self.assertEqual(used, ["bad", "good"])  # separate Translator instance starts from the first key again

    def test_falls_back_to_second_provider(self):
        d = with_key(with_key(cfg(active_provider="gemini"), "gemini", "g"), "groq", "q")
        used = []

        def caller(pdef, pc, key, prompt):
            used.append(pdef["id"])
            if pdef["id"] == "gemini":
                raise core.OCRError("HTTP 500: down")
            return '{"1": "x"}'
        src = self.write("one\n")
        res = c.clean_file(src, translate=True, translator=self.translator(d, caller=caller), clock=FIXED)
        self.assertEqual(used, ["gemini", "groq"])
        self.assertEqual(res.provider_name, "Groq")

    def test_skip_provider_error_moves_on_without_trying_other_keys(self):
        d = with_key(with_key(cfg(active_provider="gemini"), "gemini", "g1, g2"), "groq", "q")
        used = []

        def caller(pdef, pc, key, prompt):
            used.append((pdef["id"], key))
            if pdef["id"] == "gemini":
                raise core.OCRError("HTTP 404: model gone", skip_provider=True)
            return '{"1": "x"}'
        c.clean_file(self.write("one\n"), translate=True, translator=self.translator(d, caller=caller), clock=FIXED)
        self.assertEqual(used, [("gemini", "g1"), ("groq", "q")])

    def test_cancel_before_and_during(self):
        src = self.write("\n".join(f"w{i}" for i in range(c.Translator.MAX_ITEMS * 2)))
        with self.assertRaises(c.CleanerCancelled):
            c.clean_file(src, translate=True, translator=self.translator(), cancel=lambda: True)
        flag = {"stop": False}
        base = echo_caller()

        def caller(pdef, pc, key, prompt):
            flag["stop"] = True
            return base(pdef, pc, key, prompt)
        with self.assertRaises(c.CleanerCancelled):
            c.clean_file(src, translate=True, translator=self.translator(caller=caller), cancel=lambda: flag["stop"])
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_english_file_skips_translation_without_calling_anything(self):
        caller = echo_caller()
        res = c.clean_file(self.write("hello\n"), translate=True, language="English",
                           translator=self.translator(caller=caller), clock=FIXED)
        self.assertEqual(caller.calls, [])
        self.assertFalse(res.translated)
        self.assertEqual(table_rows(res.markdown)[0], ["#", "Sentence"])
        self.assertIn("no English column was added", res.markdown)
        self.assertTrue(res.notes)

    def test_too_many_entries_is_refused_before_any_request(self):
        src = self.write("\n".join(f"w{i}" for i in range(20)))
        caller = echo_caller()
        with mock.patch.object(c, "MAX_TRANSLATE_ENTRIES", 10):
            with self.assertRaises(c.CleanerError) as cm:
                c.clean_file(src, translate=True, translator=self.translator(caller=caller))
        self.assertIn("20 entries", str(cm.exception))
        self.assertEqual(caller.calls, [])
        self.assertEqual(list(self.dir.glob("*_table*")), [])

    def test_no_provider_gives_actionable_error_and_no_file(self):
        src = self.write("hello\n")
        with self.assertRaises(c.CleanerError) as cm:
            c.clean_file(src, translate=True, cfg=cfg(), clock=FIXED)     # fresh config: only Local OCR
        self.assertIn("Local OCR can't translate", str(cm.exception))
        self.assertEqual(list(self.dir.glob("*_table*")), [])
        res = c.clean_file(src, translate=False, cfg=cfg(), clock=FIXED)  # and without translate it works
        self.assertEqual(table_rows(res.markdown)[0], ["#", "Sentence"])


class ProviderChoiceTests(unittest.TestCase):
    def test_local_is_never_used_to_translate(self):
        d = with_key(cfg(active_provider="local"))
        self.assertEqual(c.Translator(d).chain(), ["gemini"])

    def test_active_cloud_first_then_fallbacks(self):
        d = with_key(with_key(cfg(active_provider="groq"), "groq", "q"), "gemini", "g")
        self.assertEqual(c.Translator(d).chain(), ["groq", "gemini"])
        d["fallback"] = False
        self.assertEqual(c.Translator(d).chain(), ["groq"])

    def test_fallback_off_and_local_active_means_nothing_automatic(self):
        d = with_key(cfg(active_provider="local", fallback=False))
        tr = c.Translator(d)
        self.assertEqual(tr.chain(), [])
        self.assertIn("Local OCR can't translate", tr.problem())
        self.assertEqual(c.Translator(d, "gemini").chain(), ["gemini"])  # explicit choice still works

    def test_explicit_provider(self):
        d = with_key(with_key(cfg(), "groq", "q"), "gemini", "g")
        self.assertEqual(c.Translator(d, "groq").chain(), ["groq"])
        self.assertEqual(c.Translator(d, "mistral").chain(), [])         # no key for it
        self.assertIn("not ready", c.Translator(d, "mistral").problem())
        self.assertEqual(c.Translator(d, "local").chain(), [])
        self.assertEqual(c.Translator(d, "nonsense").chain(), [])

    def test_paid_provider_is_not_picked_automatically(self):
        d = with_key(cfg(active_provider="local"), "deepseek", "d")
        self.assertEqual(c.Translator(d).chain(), [])

    def test_available_lists_only_ready_cloud_providers(self):
        d = with_key(cfg(), "groq", "q")
        names = dict(c.Translator.available(d))
        self.assertIn("groq", names)
        self.assertNotIn("local", names)
        self.assertNotIn("gemini", names)                                # no key
        self.assertIn("ollama", names)                                   # needs no key


class TextCallTests(unittest.TestCase):
    """call_provider_text: the request really is text-only, errors map like the image call."""

    def fake_response(self, status=200, payload=None):
        r = mock.Mock()
        r.status_code = status
        r.json.return_value = payload if payload is not None else {}
        r.text = json.dumps(payload)
        return r

    def test_gemini_request_shape(self):
        pdef, pc = core.PROVIDER_BY_ID["gemini"], {"model": "models/gemini-x"}
        ok = self.fake_response(payload={"candidates": [{"content": {"parts": [{"text": "he"}, {"text": "llo"}]}}]})
        with mock.patch.object(core.requests, "post", return_value=ok) as post:
            out = core.call_provider_text(pdef, pc, "KEY", "translate this")
        self.assertEqual(out, "hello")
        url, kw = post.call_args[0][0], post.call_args[1]
        self.assertTrue(url.endswith("/models/gemini-x:generateContent"))
        self.assertEqual(kw["headers"]["x-goog-api-key"], "KEY")
        parts = kw["json"]["contents"][0]["parts"]
        self.assertEqual(parts, [{"text": "translate this"}])                 # no inline_data
        self.assertNotIn("KEY", json.dumps(kw["json"]))

    def test_openai_request_shape(self):
        pdef, pc = core.PROVIDER_BY_ID["groq"], {"model": "m", "base_url": "https://api.groq.com/openai/v1/"}
        ok = self.fake_response(payload={"choices": [{"message": {"content": "hi"}}]})
        with mock.patch.object(core.requests, "post", return_value=ok) as post:
            out = core.call_provider_text(pdef, pc, "KEY", "p")
        self.assertEqual(out, "hi")
        self.assertEqual(post.call_args[0][0], "https://api.groq.com/openai/v1/chat/completions")
        kw = post.call_args[1]
        self.assertEqual(kw["headers"]["Authorization"], "Bearer KEY")
        self.assertEqual(kw["json"]["messages"], [{"role": "user", "content": "p"}])

    def test_keyless_provider_sends_no_auth_header(self):
        pdef, pc = core.PROVIDER_BY_ID["ollama"], {"model": "m", "base_url": "http://localhost:11434/v1"}
        ok = self.fake_response(payload={"choices": [{"message": {"content": "hi"}}]})
        with mock.patch.object(core.requests, "post", return_value=ok) as post:
            core.call_provider_text(pdef, pc, "", "p")
        self.assertNotIn("Authorization", post.call_args[1]["headers"])

    def test_errors(self):
        pdef, pc = core.PROVIDER_BY_ID["groq"], {"model": "m", "base_url": "https://x/v1"}
        with mock.patch.object(core.requests, "post", return_value=self.fake_response(429, {"error": {"message": "slow down SECRET-KEY-123"}})):
            with self.assertRaises(core.OCRError) as cm:
                core.call_provider_text(pdef, pc, "SECRET-KEY-123", "p")
        self.assertIn("HTTP 429", str(cm.exception))
        self.assertNotIn("SECRET-KEY-123", str(cm.exception))                           # the key is redacted
        self.assertFalse(cm.exception.skip_provider)
        with mock.patch.object(core.requests, "post", return_value=self.fake_response(404, {"error": "gone"})):
            with self.assertRaises(core.OCRError) as cm:
                core.call_provider_text(pdef, pc, "SECRET-KEY-123", "p")
        self.assertTrue(cm.exception.skip_provider)
        with mock.patch.object(core.requests, "post", side_effect=core.requests.exceptions.ConnectionError()):
            with self.assertRaises(core.OCRError) as cm:
                core.call_provider_text(pdef, pc, "SECRET-KEY-123", "p")
        self.assertIn("network error", str(cm.exception))
        with self.assertRaises(core.OCRError):
            core.call_provider_text(pdef, {"model": ""}, "SECRET-KEY-123", "p")
        with self.assertRaises(core.OCRError) as cm:
            core.call_provider_text(core.PROVIDER_BY_ID["local"], {"model": "small"}, "", "p")
        self.assertTrue(cm.exception.skip_provider)

    def test_full_stack_through_the_http_layer(self):
        """clean_file -> Translator -> call_provider_text -> (mocked) requests.post, no fake caller."""
        def fake_post(url, headers=None, json=None, timeout=None):
            text = json["contents"][0]["parts"][0]["text"] if "contents" in json else None
            items = __import__("json").loads(text.split("\n\n")[-1])
            reply = __import__("json").dumps({k: v.upper() for k, v in items.items()})
            return self.fake_response(payload={"candidates": [{"content": {"parts": [{"text": reply}]}}]})
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "o.txt"
            src.write_text("hola\nadios\n", encoding="utf-8")
            with mock.patch.object(core.requests, "post", side_effect=fake_post):
                res = c.clean_file(src, translate=True, language="Spanish", cfg=with_key(cfg(), "gemini", "K"),
                                   clock=FIXED)
            self.assertEqual([r[2] for r in table_rows(res.path.read_text(encoding="utf-8"))[1:]], ["HOLA", "ADIOS"])


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        d = core.default_config()
        self.assertEqual((d["cleaner_language"], d["cleaner_translate"], d["cleaner_provider"]),
                         ("Auto-detect", False, "auto"))

    def test_saved_values_survive_a_reload_and_old_configs_still_load(self):
        d = core.default_config()
        core._merge(d, {"cleaner_language": "Japanese", "cleaner_translate": True, "cleaner_provider": "groq"})
        self.assertEqual((d["cleaner_language"], d["cleaner_translate"], d["cleaner_provider"]), ("Japanese", True, "groq"))
        old = core.default_config()
        core._merge(old, {"language": "German"})        # a config.json from before this feature
        self.assertEqual(old["cleaner_language"], "Auto-detect")

    def test_language_choices_include_auto_and_english(self):
        self.assertEqual(core.LANGUAGES[0], "Auto-detect")
        self.assertIn("English", core.LANGUAGES)
        self.assertTrue(c.is_english("English") and not c.is_english("Auto-detect") and not c.is_english("German"))


if __name__ == "__main__":
    unittest.main()
