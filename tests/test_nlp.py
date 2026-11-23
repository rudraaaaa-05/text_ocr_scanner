"""Tests for ocr_nlp.py: wrapped lines, sentence splitting, furigana, word extraction. No network, no Qt.

Japanese word splitting normally needs SudachiPy, which may not be installed. The word rules are tested with
FakeTokenizer (a tiny lexicon that returns tokens the way SudachiPy would), and the SudachiPy adapter is tested
against a fake `sudachipy` module that has the same API. Neither proves SudachiPy itself behaves; see the note in
test_adapter_*.
"""
import importlib.machinery
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import ocr_nlp as n  # noqa: E402

SAMPLE = (HERE / "data" / "user_sample_raw.txt").read_text(encoding="utf-8").splitlines()

NOUN = ("名詞", "普通名詞", "一般", "*", "*", "*")
LEXICON = {
    "こないだ": ("こないだ", ("名詞", "普通名詞", "副詞可能"), "コナイダ"),
    "カフェー": ("カフェー", NOUN, "カフェー"),
    "に": ("に", ("助詞", "格助詞"), "ニ"),
    "は": ("は", ("助詞", "係助詞"), "ハ"),
    "を": ("を", ("助詞", "格助詞"), "ヲ"),
    "いった": ("行く", ("動詞", "非自立可能"), "イッタ"),
    "行く": ("行く", ("動詞", "非自立可能"), "イク"),
    "覚え": ("覚える", ("動詞", "一般"), "オボエ"),
    "覚える": ("覚える", ("動詞", "一般"), "オボエル"),
    "て": ("て", ("助詞", "接続助詞"), "テ"),
    "いない": ("いる", ("動詞", "非自立可能"), "イナイ"),
    "です": ("です", ("助動詞",), "デス"),
    "。": ("。", ("補助記号", "句点"), "。"),
    "ヘレナ": ("ヘレナ", ("名詞", "固有名詞", "人名", "名"), "ヘレナ"),
    "状況": ("状況", NOUN, "ジョウキョウ"),
    "会社員": ("会社員", NOUN, "カイシャイン"),
    "応接室": ("応接室", NOUN, "オウセツシツ"),
    "37": ("37", ("名詞", "数詞"), "サンジュウナナ"),
    "How": ("How", NOUN, "ハウ"),
    "お": ("お", ("接頭辞",), "オ"),
    "茶": ("茶", NOUN, "チャ"),
    "お茶": ("お茶", NOUN, "オチャ"),
}


class FakeTokenizer:
    """Greedy longest match over LEXICON; anything else is an unknown one-character token."""

    def tokenize(self, text):
        out, i = [], 0
        keys = sorted(LEXICON, key=len, reverse=True)
        while i < len(text):
            for k in keys:
                if text.startswith(k, i):
                    lemma, pos, reading = LEXICON[k]
                    out.append(n.Token(k, lemma, pos, reading))
                    i += len(k)
                    break
            else:
                out.append(n.Token(text[i], text[i], ("未知語",), text[i]))
                i += 1
        return out

    def reading(self, text):
        return "".join(t.reading for t in self.tokenize(text))


def sentences(text):
    return n.split_sentences(text)


class CharacterTests(unittest.TestCase):
    def test_scripts(self):
        self.assertTrue(n.is_han("漢") and n.is_han("々") and not n.is_han("あ") and not n.is_han("a"))
        self.assertTrue(n.is_hiragana("あ") and not n.is_hiragana("ア"))
        self.assertTrue(n.is_katakana("ア") and n.is_katakana("ﾊ") and not n.is_katakana("あ"))
        self.assertTrue(n.is_kana("ー") and n.is_wide_text("。") and n.is_wide_text("！") and not n.is_wide_text("."))

    def test_width_and_kana_conversion(self):
        self.assertEqual(n.display_width("猫abc"), 5)
        self.assertEqual(n.hira_to_kata("じょう"), "ジョウ")
        self.assertEqual(n.kata_to_hira("ジョウ"), "じょう")


class SentenceSplitTests(unittest.TestCase):
    def test_the_users_example_is_three_rows(self):
        self.assertEqual(sentences("こないだカフェーにいった。どこでしたか？。覚えていない。"),
                         ["こないだカフェーにいった。", "どこでしたか？。", "覚えていない。"])

    def test_cases(self):
        cases = [
            ("彼は「行く。待って。」と言った。", ["彼は「行く。待って。」と言った。"]),   # a quote is never cut in half
            ("「ありがとう。」", ["「ありがとう。」"]),
            ("彼は「行く。」と言った。次の日。", ["彼は「行く。」と言った。", "次の日。"]),
            ("彼は「行く。待って。", ["彼は「行く。", "待って。"]),                    # bracket never closed: split anyway
            ("本当ですか？！はい。", ["本当ですか？！", "はい。"]),
            ("いい?機械は苦手。", ["いい?", "機械は苦手。"]),                           # ASCII ? inside Japanese text
            ("どうも.ありがとう.", ["どうも.", "ありがとう."]),                         # OCR read 。 as a dot
            ("Hello world. This is a test! Is it? Yes.", ["Hello world.", "This is a test!", "Is it?", "Yes."]),
            ("Mr. Smith went to Washington. He liked it.", ["Mr. Smith went to Washington.", "He liked it."]),
            ("Version 3.5 is out. See No.37 and e.g. this.", ["Version 3.5 is out.", "See No.37 and e.g. this."]),
            ("1. First item", ["1. First item"]),                                    # a list number is not a sentence
            ("(1) Item two. Then more.", ["(1) Item two.", "Then more."]),
            ("see http://a.com/x?id=5 now", ["see http://a.com/x?id=5 now"]),
            ("Wait... Then he left.", ["Wait... Then he left."]),
            ("He said \"Go.\" Then left.", ["He said \"Go.\"", "Then left."]),
            ("No terminator at all", ["No terminator at all"]),
            ("   ", []),
            ("", []),
        ]
        for text, want in cases:
            with self.subTest(text=text):
                self.assertEqual(sentences(text), want)

    def test_nothing_is_lost_or_invented(self):
        for text in ("こないだカフェーにいった。どこでしたか？。覚えていない。", "彼は「行く。待って。」と言った。次。",
                     "Hello world. This is a test! Is it? Yes.", "a.b c.d. E f."):
            with self.subTest(text=text):
                self.assertEqual("".join("".join(sentences(text)).split()), "".join(text.split()))


class JoinLinesTests(unittest.TestCase):
    def join(self, *texts):
        return [c.text for c in n.join_lines([n.Line(t) for t in texts])]

    def test_terminated_lines(self):
        self.assertTrue(n.is_terminated("こんにちは。") and n.is_terminated("Done.") and n.is_terminated("「はい。」"))
        self.assertTrue(n.is_terminated("Why?") and n.is_terminated("Wait…") and n.is_terminated("状況："))
        self.assertFalse(n.is_terminated("まだ続く") or n.is_terminated("no end") or n.is_terminated(""))

    def test_english_paragraph_wrapped_over_three_lines(self):
        out = self.join("The quick brown fox jumps over the lazy dog and keeps",
                        "running through the forest until it reaches the river",
                        "bank.")
        self.assertEqual(out, ["The quick brown fox jumps over the lazy dog and keeps running through the forest "
                               "until it reaches the river bank."])

    def test_japanese_wrap_is_glued_without_a_space(self):
        out = self.join("上司に言われて、来客にお茶を出しに応接室に入りましたがそのまま立って", "いました。")
        self.assertEqual(out, ["上司に言われて、来客にお茶を出しに応接室に入りましたがそのまま立っていました。"])

    def test_a_heading_is_not_glued_to_the_paragraph_after_it(self):
        self.assertEqual(len(self.join("Chapter One", "The quick brown fox jumps over the lazy dog today.")), 2)

    def test_short_subtitle_lines_stay_separate(self):
        self.assertEqual(self.join("I don't know", "what to do"), ["I don't know", "what to do"])

    def test_long_unfinished_heading_followed_by_a_short_label(self):
        out = self.join("(1）目上の人や親しくない人に申し出をするとき", "トラックNo.37")
        self.assertEqual(len(out), 2)

    def test_a_new_list_item_is_never_glued_on(self):
        long = "x" * 60
        for item in ("1. Next item.", "(2) Next item.", "- bullet.", "① Next.", "・ 次。"):
            with self.subTest(item=item):
                self.assertEqual(len(self.join(long, item)), 2)

    def test_line_ending_in_a_comma_continues_even_when_short(self):
        self.assertEqual(self.join("来客にお、", "茶を出しました。"), ["来客にお、茶を出しました。"])

    def test_terminated_line_ends_the_chunk(self):
        self.assertEqual(self.join("一行目です。", "二行目です。"), ["一行目です。", "二行目です。"])

    def test_first_timestamp_of_a_chunk_is_kept(self):
        lines = [n.Line("あ" * 30, "10:00:01"), n.Line("い。", "10:00:05")]
        chunks = n.join_lines(lines)
        self.assertEqual((len(chunks), chunks[0].stamp, chunks[0].lines), (1, "10:00:01", 2))

    def test_glue(self):
        self.assertEqual(n.glue("inter-", "national"), "international")
        self.assertEqual(n.glue("well-", "Known"), "well- Known")
        self.assertEqual(n.glue("iPhone", "を買う"), "iPhoneを買う")
        self.assertEqual(n.glue("foo", "bar"), "foo bar")
        self.assertEqual(n.glue("", "x"), "x")

    def test_empty_and_single(self):
        self.assertEqual(n.join_lines([]), [])
        self.assertEqual(self.join("one"), ["one"])


class FuriganaTests(unittest.TestCase):
    def lines(self, *texts):
        return [n.Line(t) for t in texts]

    def test_what_looks_like_furigana(self):
        for t in ("じょう", "とりあつかい", "おうせつしっ", "きょうかしょー"):
            self.assertTrue(n.looks_like_furigana(t), t)
        for t in ("あ", "うぞ。", "ありがとう！", "漢字", "カタカナ", "ひらがなひらがなひらがな", "abc", ""):
            self.assertFalse(n.looks_like_furigana(t), t)

    def test_the_users_file_without_a_tokenizer(self):
        kept, removed = n.drop_furigana(self.lines(*SAMPLE))
        self.assertEqual(removed, ["じょう", "おうせつしっ", "とりあつかい"])
        texts = [ln.text for ln in kept]
        self.assertIn("きり", texts)              # a real heading that only looks like furigana
        self.assertIn("うぞ。", texts)             # has punctuation: a real line
        self.assertEqual(len(kept), len(SAMPLE) - 3)

    def test_short_words_before_short_lines_are_kept_without_a_tokenizer(self):
        kept, removed = n.drop_furigana(self.lines("ありがとう", "田中"))
        self.assertEqual((len(kept), removed), (2, []))

    def test_no_kanji_next_means_not_furigana(self):
        kept, removed = n.drop_furigana(self.lines("ありがとう", "Hello there my friend how are you"))
        self.assertEqual(removed, [])

    def test_last_line_and_consecutive_furigana(self):
        kept, removed = n.drop_furigana(self.lines("かんじ", "よみ", "漢字の読みを教えてください。"))
        self.assertEqual(removed, ["かんじ", "よみ"])
        self.assertEqual(n.drop_furigana(self.lines("じょう"))[1], [])

    def test_reading_check_with_a_tokenizer_is_stricter(self):
        tok = FakeTokenizer()
        # 'ありがとう' would be dropped by the length rule above a long line, but it is not the reading of 状況...
        kept, removed = n.drop_furigana(self.lines("ありがとう", "状況はヘレナが会社員ですのでそうです。"), lambda: tok)
        self.assertEqual(removed, [])
        kept, removed = n.drop_furigana(self.lines("じょう", "状況は会社員です。"), lambda: tok)
        self.assertEqual(removed, ["じょう"])

    def test_reading_check_tolerates_one_ocr_mistake(self):
        tok = FakeTokenizer()
        self.assertTrue(n.reading_matches("おうせつしっ", "お茶を出しに応接室に入りました。", tok))   # しっ vs しつ
        self.assertTrue(n.reading_matches("おうせつしつ", "応接室", tok))
        self.assertFalse(n.reading_matches("きり", "例文", tok))
        self.assertFalse(n.reading_matches("じょう", "xyz", tok))

    def test_tokenizer_is_only_started_when_needed_and_only_once(self):
        calls = []

        def getter():
            calls.append(1)
            return FakeTokenizer()
        n.drop_furigana(self.lines("Hello", "world", "もうおわりです。"), getter)
        self.assertEqual(calls, [])
        n.drop_furigana(self.lines("じょう", "状況は会社員です。", "かんじ", "漢字です。"), getter)
        self.assertEqual(calls, [1])


class WordTests(unittest.TestCase):
    def test_keep_token_rules(self):
        tok = lambda s, lemma, pos: n.Token(s, lemma, pos, "")   # noqa: E731
        self.assertTrue(n.keep_token(tok("猫", "猫", NOUN)))
        self.assertTrue(n.keep_token(tok("カフェー", "カフェー", NOUN)))
        for bad in (tok("に", "に", ("助詞",)), tok("です", "です", ("助動詞",)), tok("。", "。", ("補助記号",)),
                    tok("37", "37", ("名詞", "数詞")), tok("ヘレナ", "ヘレナ", ("名詞", "固有名詞", "人名", "名")),
                    tok("How", "How", NOUN), tok("お", "お", ("接頭辞",)), tok("猫", "", NOUN), tok("猫", "猫", ())):
            self.assertFalse(n.keep_token(bad), bad)

    def test_japanese_words_use_dictionary_form_and_reading(self):
        words = n.extract_words(["こないだカフェーにいった。", "お茶を覚えていない。"], "Japanese", FakeTokenizer())
        by_word = {w.word: w for w in words}
        self.assertEqual([w.word for w in words], ["こないだ", "カフェー", "行く", "お茶", "覚える", "いる"])
        self.assertEqual(by_word["行く"].reading, "いく")             # the reading of the DICTIONARY form, not いった
        self.assertEqual(by_word["こないだ"].reading, "")            # no kanji, no reading needed
        self.assertEqual(by_word["カフェー"].reading, "")
        self.assertEqual(by_word["お茶"].reading, "おちゃ")
        self.assertEqual(by_word["行く"].sentence, "こないだカフェーにいった。")

    def test_japanese_drops_particles_names_numbers_and_latin(self):
        words = n.extract_words(["ヘレナは状況を会社員です。How 37"], "Japanese", FakeTokenizer())
        self.assertEqual([w.word for w in words], ["状況", "会社員"])

    def test_repeats_are_counted_not_listed_twice_and_keep_the_first_sentence(self):
        words = n.extract_words(["行くです。", "行く。行く。"], "Japanese", FakeTokenizer())
        self.assertEqual([(w.word, w.count, w.sentence) for w in words], [("行く", 3, "行くです。")])

    def test_spaced_languages_need_no_tokenizer(self):
        words = n.extract_words(["The cat saw the Cat.", "It's 42 cats! N2 e-mail"], "English")
        self.assertEqual([w.word for w in words], ["the", "cat", "saw", "it's", "cats", "n2", "e-mail"])
        self.assertEqual(words[0].count, 2)
        self.assertEqual(words[1].count, 2)
        self.assertEqual(words[3].sentence, "It's 42 cats! N2 e-mail")
        self.assertTrue(all(w.reading == "" for w in words))

    def test_other_languages_are_split_on_words_too(self):
        words = n.extract_words(["Ça va très bien, merci beaucoup."], "French")
        self.assertEqual([w.word for w in words], ["ça", "va", "très", "bien", "merci", "beaucoup"])

    def test_languages_without_spaces_say_so_instead_of_guessing(self):
        for lang in ("Chinese (Simplified)", "Chinese (Traditional)", "Thai"):
            with self.assertRaises(n.WordSplitError) as cm:
                n.extract_words(["你好吗"], lang)
            self.assertIn("sentence mode", str(cm.exception))

    def test_auto_detect(self):
        self.assertEqual(n.word_language("Auto-detect", ["今日は天気がいい"]), "japanese")      # kana present
        self.assertEqual(n.word_language("Auto-detect", ["Hello there"]), "spaced")
        self.assertEqual(n.word_language("", ["Bonjour"]), "spaced")
        with self.assertRaises(n.WordSplitError) as cm:
            n.word_language("Auto-detect", ["你好吗"])                                           # kanji only
        self.assertIn("choose Japanese", str(cm.exception))

    def test_japanese_without_the_splitter_explains_how_to_install_it(self):
        n._cache["tokenizer"] = None
        with mock.patch.object(n, "tokenizer_problem", return_value="needs SudachiPy: pip install sudachipy"):
            with self.assertRaises(n.WordSplitError) as cm:
                n.extract_words(["日本語です。"], "Japanese")
            self.assertIn("pip install", str(cm.exception))
            self.assertIsNone(n.optional_tokenizer())

    def test_tokenizer_problem_names_the_right_command(self):
        with mock.patch("importlib.util.find_spec", return_value=None):
            msg = n.tokenizer_problem()
        self.assertIn("pip install sudachipy sudachidict_core", msg)
        self.assertIn(sys.executable, msg)
        with mock.patch("importlib.util.find_spec", return_value=object()):
            self.assertIsNone(n.tokenizer_problem())


class SudachiAdapterTests(unittest.TestCase):
    """The adapter is checked against a fake `sudachipy` with the same API. This proves the adapter calls the
    library the way its documentation says; it does NOT prove how real SudachiPy tokenises."""

    def make_fake(self, fail_on_create=False):
        calls = {}

        class Morpheme:
            def __init__(self, surface, lemma, pos, reading):
                self._v = (surface, lemma, pos, reading)

            def surface(self): return self._v[0]
            def dictionary_form(self): return self._v[1]
            def part_of_speech(self): return list(self._v[2])
            def reading_form(self): return self._v[3]

        class Tok:
            def tokenize(self, text, mode=None):
                calls["mode"] = mode
                return [Morpheme(text, "行く", ("動詞", "一般"), "イク")]

        class Dictionary:
            def create(self):
                if fail_on_create:
                    raise RuntimeError("dictionary not found")
                return Tok()

        mode_b = object()
        sd = types.ModuleType("sudachipy")
        sd.dictionary = types.SimpleNamespace(Dictionary=Dictionary)
        sd.tokenizer = types.SimpleNamespace(Tokenizer=types.SimpleNamespace(SplitMode=types.SimpleNamespace(B=mode_b)))
        for name in ("sudachipy", "sudachidict_core"):
            m = sd if name == "sudachipy" else types.ModuleType(name)
            m.__spec__ = importlib.machinery.ModuleSpec(name, None)
            sys.modules[name] = m
        return calls, mode_b

    def setUp(self):
        self._saved = {k: sys.modules.get(k) for k in ("sudachipy", "sudachidict_core")}
        n._cache["tokenizer"] = None
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        n._cache["tokenizer"] = None

    def test_adapter_maps_morphemes_and_uses_the_middle_split_mode(self):
        calls, mode_b = self.make_fake()
        self.assertIsNone(n.tokenizer_problem())
        tok = n.default_tokenizer()
        self.assertIs(n.default_tokenizer(), tok)                              # created once, then shared
        out = tok.tokenize("いった")
        self.assertEqual(out, [n.Token("いった", "行く", ("動詞", "一般"), "イク")])
        self.assertIs(calls["mode"], mode_b)
        self.assertEqual(tok.reading("いった"), "イク")

    def test_full_pipeline_through_the_adapter(self):
        self.make_fake()
        words = n.extract_words(["行った。"], "Japanese")
        self.assertEqual([(w.word, w.reading) for w in words], [("行く", "いく")])

    def test_a_dictionary_that_will_not_load_is_reported_with_its_reason(self):
        self.make_fake(fail_on_create=True)
        with self.assertRaises(n.WordSplitError) as cm:
            n.default_tokenizer()
        self.assertIn("dictionary not found", str(cm.exception))
        self.assertIsNone(n.optional_tokenizer())


if __name__ == "__main__":
    unittest.main()
