from __future__ import annotations

import difflib
import importlib.util
import re
import sys
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------------------------------------
# characters
# --------------------------------------------------------------------------------------------------------------


def is_han(ch: str) -> bool:
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF
            or 0x20000 <= o <= 0x2FA1F or ch == "\u3005")


def is_hiragana(ch: str) -> bool:
    return 0x3041 <= ord(ch) <= 0x309F


def is_katakana(ch: str) -> bool:
    o = ord(ch)
    return 0x30A1 <= o <= 0x30FA or 0x30FD <= o <= 0x30FF or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9D


def is_kana(ch: str) -> bool:
    return is_hiragana(ch) or is_katakana(ch) or ch == "\u30fc"


def is_cjk(ch: str) -> bool:
    return is_han(ch) or is_kana(ch)


def is_wide_text(ch: str) -> bool:
    """CJK letters plus CJK / full-width punctuation: text written without spaces between words."""
    o = ord(ch)
    return is_cjk(ch) or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF


def display_width(s: str) -> int:
    """Terminal-style width: full-width characters count 2, combining marks 0."""
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def hira_to_kata(s: str) -> str:
    return "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c for c in s)


def kata_to_hira(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if 0x30A1 <= ord(c) <= 0x30F6 else c for c in s)


# --------------------------------------------------------------------------------------------------------------
# romaji -> hiragana (typing Japanese without an input method: the dictionary search box has a switch for it)
# --------------------------------------------------------------------------------------------------------------

def _romaji_table() -> dict:
    t = {}
    rows = {"": "あいうえお", "k": "かきくけこ", "s": "さしすせそ", "t": "たちつてと", "n": "なにぬねの", "h": "はひふへほ",
            "m": "まみむめも", "r": "らりるれろ", "g": "がぎぐげご", "z": "ざじずぜぞ", "d": "だぢづでど", "b": "ばびぶべぼ",
            "p": "ぱぴぷぺぽ"}
    for c, kana in rows.items():
        for v, k in zip("aiueo", kana):
            t[c + v] = k
        if c:                                           # kya kyu kyo, sya, tya, ...
            for v, small in zip("auo", "ゃゅょ"):
                t[c + "y" + v] = kana[1] + small
    t.update({"ya": "や", "yu": "ゆ", "yo": "よ", "wa": "わ", "wo": "を", "wi": "うぃ", "we": "うぇ", "vu": "ゔ",
              "shi": "し", "sha": "しゃ", "shu": "しゅ", "sho": "しょ", "she": "しぇ",
              "chi": "ち", "cha": "ちゃ", "chu": "ちゅ", "cho": "ちょ", "che": "ちぇ", "tsu": "つ",
              "fu": "ふ", "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ",
              "ji": "じ", "ja": "じゃ", "ju": "じゅ", "jo": "じょ", "je": "じぇ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ",
              "cya": "ちゃ", "cyu": "ちゅ", "cyo": "ちょ", "shya": "しゃ", "shyu": "しゅ", "shyo": "しょ", "chya": "ちゃ", "chyu": "ちゅ", "chyo": "ちょ", "thi": "てぃ", "dhi": "でぃ", "tsa": "つぁ"})
    for pre in "xl":                                    # small kana: xa la xtu ltu xya ...
        for v, k in zip("aiueo", "ぁぃぅぇぉ"):
            t[pre + v] = k
        for v, k in zip("auo", "ゃゅょ"):
            t[pre + "y" + v] = k
        t[pre + "tu"] = t[pre + "tsu"] = "っ"
        t[pre + "wa"] = "ゎ"
    return t


_ROMAJI = _romaji_table()
_ROMAJI_MAX = max(len(k) for k in _ROMAJI)
_VOWELS = "aiueo"
_DOUBLES = set("bcdfghjkpqrstvwxyz")


def romaji_to_kana(text: str, final: bool = False) -> str:
    """'taberu' -> 'たべる', 'gakkou' -> 'がっこう', 'konnichiha' -> 'こんにちは', 'kin'en' -> 'きんえん'.

    Letters that cannot be a complete syllable yet are left as typed ('ky' stays 'ky' until the next letter
    arrives), so this can be applied to the text of a box after every keystroke. A lone 'n' at the end becomes 'ん'
    only with final=True (when the person presses Enter). Kana, kanji and everything else pass through; '-' after a
    kana is the long-vowel mark ー. Upper case letters are read like lower case."""
    s = text or ""
    low = s.lower()
    out: list = []
    i, n = 0, len(s)
    while i < n:
        ch = low[i]
        if not ("a" <= ch <= "z" or ch in "'-"):
            out.append(s[i])
            i += 1
            continue
        if ch == "-":
            out.append("ー" if out and is_kana(out[-1]) else s[i])
            i += 1
            continue
        if ch == "'":
            out.append(s[i])
            i += 1
            continue
        if ch in _DOUBLES and i + 1 < n and low[i + 1] == ch:      # kk, tt, ss ... -> small tsu + the letter
            out.append("っ")
            i += 1
            continue
        if ch == "t" and low[i + 1:i + 3] == "ch":                 # tchi -> っち
            out.append("っ")
            i += 1
            continue
        if ch == "n":
            nxt = low[i + 1] if i + 1 < n else ""
            nxt2 = low[i + 2] if i + 2 < n else ""
            if nxt == "'":
                out.append("ん")
                i += 2
                continue
            if nxt == "n":
                if nxt2 and (nxt2 in _VOWELS or nxt2 == "y"):      # konnichi: the first n is ん, the second starts に
                    out.append("ん")
                    i += 1
                else:                                              # nn is ん
                    out.append("ん")
                    i += 2
                continue
        for length in range(min(_ROMAJI_MAX, n - i), 0, -1):
            kana = _ROMAJI.get(low[i:i + length])
            if kana:
                out.append(kana)
                i += length
                break
        else:
            if ch == "n":
                nxt = low[i + 1] if i + 1 < n else ""
                if (not nxt and final) or (nxt and nxt not in _VOWELS and nxt != "y"):
                    out.append("ん")
                    i += 1
                    continue
                if nxt == "y" and i + 2 < n:                       # ny + something that is not a/u/o
                    out.append("ん")
                    i += 1
                    continue
            out.append(s[i])
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------------------------------------------
# sentence splitting
# --------------------------------------------------------------------------------------------------------------

_ENDERS = "\u3002\uff0e\uff01\uff1f!?"            # 。 ． ！ ？ ! ?   (a plain "." has its own rules)
_OPEN = "\u300c\u300e\uff08(\uff3b[\u3010\u3008\u300a"       # 「 『 （ ( ［ [ 【 〈 《
_CLOSE = "\u300d\u300f\uff09)\uff3d]\u3011\u3009\u300b"      # 」 』 ） ) ］ ] 】 〉 》
_TRAIL = _CLOSE + "\u201d\u2019\"'"                        # what may follow the punctuation and still belong to it
# After 「...。」 these mean the sentence goes on (と言った, って, など), so the quote is not cut off from it.
_QUOTE_CONTINUES_RE = re.compile(r"^(?:\u3068(?!\u3053\u308d|\u306b\u304b\u304f|\u3082\u304b\u304f)|\u3063\u3066|\u306a\u3069|\u306a\u3093\u3066)")
_ABBREV = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "no", "fig", "vol", "approx",
    "inc", "ltd", "co", "u.s", "cf", "al", "ca", "dept", "est", "ed", "eds", "pp", "p",
})


def _end_of_punctuation(s: str, i: int, start: int):
    """If a sentence can end at s[i], the index just after its punctuation run, otherwise None."""
    n = len(s)
    ch = s[i]
    prev = s[i - 1] if i > 0 else ""
    nxt = s[i + 1] if i + 1 < n else ""
    if ch in "\u3002\uff0e\uff01\uff1f":
        j = i + 1
        while j < n and s[j] in _ENDERS:
            j += 1
        return j
    if ch in "!?":
        if prev.isascii() and prev.isalnum() and nxt.isascii() and nxt.isalnum():
            return None                                   # a URL query such as ?id=5, not a sentence end
        j = i + 1
        while j < n and s[j] in _ENDERS:
            j += 1
        return j
    if ch == ".":
        if prev and is_wide_text(prev):
            return i + 1                                  # OCR often reads 。 as a plain dot
        k = i + 1
        while k < n and s[k] in _TRAIL:
            k += 1                                        # He said "Go." Then ...: the quote belongs to the sentence
        if k < n and not s[k].isspace():
            return None                                   # No.37  3.5  a.b  ...
        if prev == "." or nxt == ".":
            return None                                   # ellipsis
        word = re.search(r"([A-Za-z][A-Za-z.]*|\d+)$", s[start:i])
        token = word.group(1).lower() if word else ""
        if token.isdigit() and s[start:i].strip().lstrip("(\uff08") == token:
            return None                                   # "1." at the start of an item is a list number
        if token in _ABBREV or (len(token) == 1 and token.isalpha()):
            return None
        rest = s[k:].lstrip()
        if rest and rest[0].islower():
            return None                                   # "e.g. something": the sentence goes on
        return i + 1
    return None


def _split(s: str, track_brackets: bool):
    parts, start, depth, i, n = [], 0, 0, 0, len(s)
    while i < n:
        ch = s[i]
        if track_brackets:
            if ch in _OPEN:
                depth += 1
            elif ch in _CLOSE and depth > 0:
                depth -= 1
        end = _end_of_punctuation(s, i, start)
        if end is None:
            i += 1
            continue
        j = end
        inside = depth > 0
        while j < n and s[j] in _TRAIL:
            if track_brackets and s[j] in _CLOSE and depth > 0:
                depth -= 1
            j += 1
        if inside and depth == 0 and _QUOTE_CONTINUES_RE.match(s[j:]):
            i = j
            continue
        if depth == 0 or not track_brackets:
            piece = s[start:j].strip()
            if piece:
                parts.append(piece)
            start = j
        i = j
    tail = s[start:].strip()
    if tail:
        parts.append(tail)
    if track_brackets and depth > 0:
        return None            # a bracket that never closes (OCR dropped it): redo without trusting brackets
    return parts


def split_sentences(text: str) -> list:
    """One string per sentence. Punctuation stays with its sentence. A quote or bracket is never cut in half."""
    s = " ".join((text or "").split())
    if not s:
        return []
    parts = _split(s, True)
    if parts is None:
        parts = _split(s, False)
    return parts


# --------------------------------------------------------------------------------------------------------------
# wrapped lines
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Line:
    text: str
    stamp: str | None = None


_TERMINATORS = "\u3002\uff0e.\uff01!\uff1f?\u2026\u2025:\uff1a"     # 。 ． . ！ ! ？ ? … ‥ : ：
_COMMAS = "\u3001,\uff0c"                                           # 、 , ，
_NEW_ITEM_RE = re.compile(
    r"^\s*(?:[(\uff08]?\d{1,3}[)\uff09.\uff0e\u3001:\uff1a]|[\u2460-\u2473\u3220-\u3229]"
    r"|[\u2022\u30fb\u25cf\u25cb\u25a0\u25a1\u25b6\u25b7\u25c6\u25c7\u25aa\-\u2013\u2014*\uff0a]\s?"
    r"|[A-Za-z][.\uff0e)\uff09]\s)")

FULL_RATIO = 0.75       # a line is "full" when it reaches this share of the widest unfinished line nearby
MIN_WRAP_WIDTH = 28     # ...and is at least this wide (14 Japanese characters or 28 Latin ones)
WINDOW = 5              # how many lines each way "nearby" covers


def is_terminated(text: str) -> bool:
    s = text.rstrip()
    while s and s[-1] in _TRAIL:
        s = s[:-1].rstrip()
    return bool(s) and s[-1] in _TERMINATORS


def _ends_with_comma(text: str) -> bool:
    s = text.rstrip()
    return bool(s) and s[-1] in _COMMAS


def glue(a: str, b: str) -> str:
    """Join two wrapped pieces: no space inside Japanese / Chinese text, a space between Latin words."""
    if not a:
        return b
    if not b:
        return a
    if len(a) > 1 and a[-1] == "-" and a[-2].isalpha() and a[-2].isascii() and b[0].islower() and b[0].isascii():
        return a[:-1] + b                      # "inter-" + "national" (a real hyphenated word keeps its hyphen only
                                               #  when the next piece starts with a capital or digit)
    if is_wide_text(a[-1]) or is_wide_text(b[0]):
        return a + b
    return a + " " + b


@dataclass
class Chunk:
    text: str
    stamp: str | None = None
    lines: int = 1


def join_lines(lines: list, *, full_ratio: float = FULL_RATIO, min_width: int = MIN_WRAP_WIDTH,
               window: int = WINDOW) -> list:
    """Glue lines that are one wrapped sentence back together. Returns a list of Chunk.

    Line i continues onto line i+1 when i does not end a sentence and either i ends with a comma, or i is a full
    width line and i+1 looks like its continuation (it ends a sentence, is full width too, or ends with a comma).
    A line that starts a new list item ("1.", "(1)", a bullet) is never glued to the one before it.
    """
    n = len(lines)
    widths = [display_width(ln.text) for ln in lines]
    done = [is_terminated(ln.text) for ln in lines]

    def continues(i: int) -> bool:
        if i + 1 >= n or done[i]:
            return False
        if _NEW_ITEM_RE.match(lines[i + 1].text):
            return False
        if _ends_with_comma(lines[i].text):
            return True
        lo, hi = max(0, i - window), min(n, i + window + 1)
        ref = max(widths[k] for k in range(lo, hi) if not done[k])
        if widths[i] < min_width or widths[i] < full_ratio * ref:
            return False
        nxt_full = widths[i + 1] >= min_width and widths[i + 1] >= full_ratio * ref
        return done[i + 1] or nxt_full or _ends_with_comma(lines[i + 1].text)

    chunks, cur = [], None
    for i, ln in enumerate(lines):
        if cur is None:
            cur = Chunk(ln.text, ln.stamp)
        else:
            cur.text = glue(cur.text, ln.text)
            cur.lines += 1
            cur.stamp = cur.stamp or ln.stamp
        if not continues(i):
            chunks.append(cur)
            cur = None
    return chunks


# --------------------------------------------------------------------------------------------------------------
# furigana
# --------------------------------------------------------------------------------------------------------------

FURIGANA_MAX_LEN = 10


def looks_like_furigana(text: str) -> bool:
    """A short line of nothing but hiragana: what small reading aids above kanji look like once OCR has read them."""
    return 2 <= len(text) <= FURIGANA_MAX_LEN and all(is_hiragana(c) or c == "\u30fc" for c in text)


def reading_matches(furigana: str, text: str, tokenizer) -> bool:
    """Is `furigana` (roughly) how some part of `text` is read? Tolerates one wrong character, as OCR makes them."""
    target = hira_to_kata(furigana)
    reading = tokenizer.reading(text)
    if not reading:
        return False
    if target in reading:
        return True
    for w in sorted({len(target) - 1, len(target), len(target) + 1}):
        if w < 2:
            continue
        for k in range(0, max(0, len(reading) - w) + 1):
            if difflib.SequenceMatcher(None, target, reading[k:k + w]).ratio() >= 0.8:
                return True
    return False


_UNSET = object()


def drop_furigana(lines: list, get_tokenizer=None):
    """-> (lines without furigana, [removed texts]).

    A line is furigana when it is a short all-hiragana line and the next real line has kanji. With a Japanese word
    splitter available the reading is checked against that line, which is reliable. Without one the next line has to
    be at least twice as long, so that short stand-alone words and headings are kept. Removed lines are always
    reported to the user, never silently lost.
    """
    kept, removed, tok = [], [], _UNSET
    for i, ln in enumerate(lines):
        if looks_like_furigana(ln.text):
            j = i + 1
            while j < len(lines) and looks_like_furigana(lines[j].text):
                j += 1
            if j < len(lines) and any(is_han(c) for c in lines[j].text):
                if tok is _UNSET:
                    tok = get_tokenizer() if get_tokenizer else None
                if tok is not None:
                    drop = reading_matches(ln.text, lines[j].text, tok)
                else:
                    drop = len(lines[j].text) >= 2 * len(ln.text)
                if drop:
                    removed.append(ln.text)
                    continue
        kept.append(ln)
    return kept, removed


# --------------------------------------------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------------------------------------------

class WordSplitError(Exception):
    """Word splitting is not possible for this text. The message is meant to be shown to the user."""


@dataclass(frozen=True)
class Token:
    surface: str
    lemma: str                  # dictionary form: 行った -> 行く
    pos: tuple                  # part of speech, most general first: ("名詞", "普通名詞", "一般", ...)
    reading: str = ""           # katakana reading of the surface


@dataclass
class Word:
    word: str
    reading: str = ""           # hiragana, only for words that contain kanji
    sentence: str = ""          # the first sentence the word was seen in
    count: int = 1
    translation: str | None = None


def tokenizer_problem():
    """None when Japanese word splitting is available, otherwise what to do about it."""
    try:
        missing = [m for m in ("sudachipy", "sudachidict_core") if importlib.util.find_spec(m) is None]
    except (ImportError, ValueError):
        missing = ["sudachipy"]
    if missing:
        return ("Japanese word splitting needs the SudachiPy package. Install it with:  "
                f"{sys.executable} -m pip install sudachipy sudachidict_core")
    return None


class SudachiTokenizer:
    """Thin adapter over SudachiPy (https://github.com/WorksApplications/SudachiPy)."""

    def __init__(self):
        from sudachipy import dictionary, tokenizer
        self._tok = dictionary.Dictionary().create()
        self._mode = tokenizer.Tokenizer.SplitMode.B       # middle-sized units: good for vocabulary
        self._lemma_readings = {}

    def tokenize(self, text: str) -> list:
        return [Token(m.surface(), m.dictionary_form(), tuple(m.part_of_speech()), m.reading_form())
                for m in self._tok.tokenize(text, self._mode)]

    def reading(self, text: str) -> str:
        return "".join(m.reading_form() for m in self._tok.tokenize(text, self._mode))


_cache = {"tokenizer": None}


def default_tokenizer():
    """The shared SudachiTokenizer. Raises WordSplitError (with install instructions) when it is not available."""
    if _cache["tokenizer"] is not None:
        return _cache["tokenizer"]
    why = tokenizer_problem()
    if why:
        raise WordSplitError(why)
    try:
        _cache["tokenizer"] = SudachiTokenizer()
    except Exception as exc:  # the dictionary package can fail to load in several ways; the user needs the reason
        raise WordSplitError(f"Japanese word splitting could not start: {exc}") from exc
    return _cache["tokenizer"]


def optional_tokenizer():
    """The shared tokenizer, or None when it is not installed or fails to start (callers have a fallback)."""
    try:
        return default_tokenizer()
    except WordSplitError:
        return None


_SKIP_POS1 = frozenset({"助詞", "助動詞", "補助記号", "空白", "記号", "接頭辞", "接尾辞"})   # particles, endings, symbols


def keep_token(tok: Token) -> bool:
    """Vocabulary worth listing: real words, not particles, endings, symbols, numbers or people's names."""
    pos = tok.pos
    if not pos or pos[0] in _SKIP_POS1:
        return False
    if pos[:2] == ("名詞", "数詞") or pos[:3] == ("名詞", "固有名詞", "人名"):
        return False
    return bool(tok.lemma.strip()) and any(is_cjk(c) for c in tok.surface)


WORD_RE = re.compile(r"[^\W_]+(?:['\u2019\-][^\W_]+)*")

_NO_SPACES = ("chinese", "thai", "lao", "khmer", "burmese")


def word_language(language: str, sentences: list) -> str:
    """'japanese' (needs the splitter), 'spaced' (words are separated by spaces) or raises WordSplitError."""
    lang = (language or "").strip().lower()
    if lang.startswith("japanese"):
        return "japanese"
    if any(lang.startswith(x) for x in _NO_SPACES):
        raise WordSplitError(f"Word-by-word mode can't split {language} text yet. Use the sentence mode instead.")
    if lang and not lang.startswith("auto"):
        return "spaced"
    text = "".join(sentences)
    if any(is_kana(c) for c in text):
        return "japanese"
    if any(is_han(c) for c in text):
        raise WordSplitError("This text looks Chinese, and word-by-word mode can't split Chinese yet. If it is "
                             "Japanese, choose Japanese as the language. Otherwise use the sentence mode.")
    return "spaced"


def extract_words(sentences: list, language: str = "Auto-detect", tokenizer=None) -> list:
    """Unique words in order of first appearance, each with the first sentence it was seen in."""
    kind = word_language(language, sentences)
    words, index = [], {}

    def add(word: str, sentence: str, reading: str = ""):
        found = index.get(word)
        if found is not None:
            found.count += 1
            return
        index[word] = w = Word(word, reading, sentence)
        words.append(w)

    if kind == "japanese":
        tok = tokenizer or default_tokenizer()
        readings = {}
        for sent in sentences:
            for t in tok.tokenize(sent):
                if not keep_token(t):
                    continue
                reading = ""
                if t.lemma not in index and any(is_han(c) for c in t.lemma):
                    if t.lemma not in readings:
                        readings[t.lemma] = kata_to_hira(tok.reading(t.lemma))
                    reading = readings[t.lemma]
                add(t.lemma, sent, reading)
    else:
        for sent in sentences:
            for m in WORD_RE.finditer(sent):
                token = m.group(0)
                if token.isdigit():
                    continue
                add(token.lower(), sent)
    return words
