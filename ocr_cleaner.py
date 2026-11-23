from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ocr_core import (
    PROVIDER_BY_ID, OCRClient, OCRError, call_provider_text, parse_keys,
)
from ocr_nlp import (  # noqa: F401  (display_width is re-exported: the table code and the tests use it from here)
    Line, WordSplitError, display_width, drop_furigana, extract_words, join_lines, optional_tokenizer,
    split_sentences,
)

MAX_SOURCE_BYTES = 50 * 1024 * 1024
MAX_TRANSLATE_ENTRIES = 2000   # one run never burns more than this many entries of somebody's free quota
PAD_CAP = 48                   # columns are padded for readability up to this display width
NOT_TRANSLATED = "_(not translated)_"
TABLE_SUFFIX = "_table"

_STAMP_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\][ \t]?(.*)$")
# zero-width characters, BOM, bidi marks and C0/C1 control characters (tab and newline are handled before)
_INVISIBLE_RE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u200e\u200f\u202a-\u202e\u2066-\u2069"
                           "\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


class CleanerError(Exception):
    """Something the user can act on. The message is meant to be shown as is."""


class CleanerCancelled(Exception):
    pass


# --------------------------------------------------------------------------------------------------------------
# reading and cleaning
# --------------------------------------------------------------------------------------------------------------

@dataclass
class Entry:
    text: str
    first_seen: str | None = None   # "2026-01-31 14:05:09" when the raw file had timestamps
    translation: str | None = None


_CJK = "\u3001-\u303f\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff"
_CJK_GAP_RE = re.compile(f"(?<=[{_CJK}])\\s+(?=[{_CJK}])")


def normalize_text(text: str) -> str:
    """One display line: invisible characters gone, every run of whitespace (incl. U+3000) collapsed to one space,
    and no space left between two Japanese / Chinese characters (those gaps are OCR artefacts, never real)."""
    text = _INVISIBLE_RE.sub("", text.replace("\t", " "))
    return _CJK_GAP_RE.sub("", " ".join(text.split()))


def has_content(text: str) -> bool:
    """True if there is at least one letter or digit (kana and kanji count). Lines of only symbols are noise."""
    return any(ch.isalnum() for ch in text)


def dedupe_key(text: str) -> str:
    """Two lines with the same key are the same text read twice: width, case, spacing and punctuation ignored."""
    t = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in t if unicodedata.category(ch)[0] in "LN")


def read_source(path) -> str:
    p = Path(os.path.expanduser(str(path)))
    if not p.exists():
        raise CleanerError(f"The file does not exist: {p}")
    if not p.is_file():
        raise CleanerError(f"This is not a file: {p}")
    try:
        size = p.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise CleanerError(f"The file is too large ({size // (1024 * 1024)} MB, limit {MAX_SOURCE_BYTES // (1024 * 1024)} MB).")
        return p.read_bytes().decode("utf-8-sig", errors="replace")
    except OSError as exc:
        raise CleanerError(f"Could not read {p}: {exc.strerror or exc}") from exc


@dataclass
class Parsed:
    entries: list = field(default_factory=list)
    lines_read: int = 0       # non-blank lines in the file
    duplicates: int = 0
    noise: int = 0
    has_times: bool = False
    furigana: list = field(default_factory=list)   # furigana lines that were left out (reported in the file)
    unit: str = "entry"                            # what one entry is: a line, or (after to_sentences) a sentence


def parse_output(text: str) -> Parsed:
    out = Parsed()
    seen = set()
    for raw in text.splitlines():
        if not raw.strip():
            continue
        out.lines_read += 1
        stamp = None
        m = _STAMP_RE.match(raw.strip())
        body = raw
        if m:
            stamp, body = m.group(1), m.group(2)
        body = normalize_text(body)
        if not body or not has_content(body):
            out.noise += 1
            continue
        key = dedupe_key(body)
        if key in seen:
            out.duplicates += 1
            continue
        seen.add(key)
        out.entries.append(Entry(body, stamp))
        if stamp:
            out.has_times = True
    return out


def to_sentences(parsed: Parsed, *, ignore_furigana: bool = True, get_tokenizer=None) -> Parsed:
    """Lines -> sentences: furigana out, wrapped lines glued together, lines with several sentences split."""
    lines = [Line(e.text, e.first_seen) for e in parsed.entries]
    removed = []
    if ignore_furigana:
        lines, removed = drop_furigana(lines, get_tokenizer)
    out = Parsed(lines_read=parsed.lines_read, duplicates=parsed.duplicates, noise=parsed.noise,
                 has_times=parsed.has_times, furigana=removed, unit="sentence")
    seen = set()
    for chunk in join_lines(lines):
        for sentence in split_sentences(chunk.text):
            if not has_content(sentence):
                continue
            key = dedupe_key(sentence)
            if key in seen:
                out.duplicates += 1
                continue
            seen.add(key)
            out.entries.append(Entry(sentence, chunk.stamp))
    return out


# --------------------------------------------------------------------------------------------------------------
# markdown table
# --------------------------------------------------------------------------------------------------------------

_ASCII_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_ENTITY_RE = re.compile(r"&(#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def cell(text: str) -> str:
    """Make OCR text safe inside one Markdown table cell, so a renderer shows exactly what was on the screen.

    Escaped: | (would end the cell), < (would be read as an HTML tag), ` * [ ] ~ (code, emphasis, links,
    strikethrough), _ at the edge of a word (snake_case is left alone), & when it would form an entity, and a
    backslash only when it sits in front of punctuation (so Windows paths stay readable).
    """
    t = " ".join(str(text).split())
    out = []
    n = len(t)
    for i, ch in enumerate(t):
        if ch == "\\":
            out.append("\\\\" if i + 1 < n and t[i + 1] in _ASCII_PUNCT else ch)
        elif ch in "|`*<[]~":
            out.append("\\" + ch)
        elif ch == "_":
            inside_word = i > 0 and i + 1 < n and t[i - 1].isalnum() and t[i + 1].isalnum()
            out.append(ch if inside_word else "\\_")
        elif ch == "&" and _ENTITY_RE.match(t, i):
            out.append("\\&")
        else:
            out.append(ch)
    return "".join(out)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = max(0, width - display_width(s))
    return (" " * gap + s) if right else (s + " " * gap)


def render_table(columns: list, rows: list, right_aligned=(0,)) -> str:
    """columns: header names; rows: lists of already-escaped cell strings. Columns are padded so the raw file reads well."""
    widths = []
    for i, name in enumerate(columns):
        longest = max([display_width(name)] + [display_width(r[i]) for r in rows])
        widths.append(max(3, min(PAD_CAP, longest)))
    head = "| " + " | ".join(_pad(n, widths[i], i in right_aligned) for i, n in enumerate(columns)) + " |"
    sep_cells = []
    for i, w in enumerate(widths):
        sep_cells.append("-" * (w - 1) + ":" if i in right_aligned else "-" * w)
    sep = "| " + " | ".join(sep_cells) + " |"
    body = ["| " + " | ".join(_pad(c, widths[i], i in right_aligned) for i, c in enumerate(r)) + " |" for r in rows]
    return "\n".join([head, sep] + body)


_MODE_LABEL = {"lines": "line by line", "sentence": "sentence by sentence", "vocab": "word by word (vocabulary list)"}


def build_markdown(entries: list, *, source_name: str, language: str, translated: bool, with_times: bool,
                   parsed: Parsed, provider_name: str = "", notes=(), clock=datetime.now, mode: str = "lines",
                   words: list | None = None) -> str:
    """`entries` are the rows of a line / sentence table. In "vocab" mode they are the sentences the `words` came from."""
    if mode == "vocab":
        has_reading = any(w.reading for w in words)
        columns = ["#", "Word"] + (["Reading"] if has_reading else []) + (["English"] if translated else []) + ["Sentence"]
        rows = []
        for n, w in enumerate(words, 1):
            row = [str(n), cell(w.word)]
            if has_reading:
                row.append(cell(w.reading))
            if translated:
                row.append(cell(w.translation) if w.translation else NOT_TRANSLATED)
            row.append(cell(w.sentence))
            rows.append(row)
        right = (0,)
    else:
        columns = ["#"] + (["Time"] if with_times else []) + ["Text" if mode == "lines" else "Sentence"] \
            + (["English"] if translated else [])
        rows = []
        for n, e in enumerate(entries, 1):
            row = [str(n)]
            if with_times:
                row.append(e.first_seen or "")
            row.append(cell(e.text))
            if translated:
                row.append(cell(e.translation) if e.translation else NOT_TRANSLATED)
            rows.append(row)
        right = (0,)
    lang = (language or "").strip() or "Auto-detect"
    info = [f"Source: `{source_name}`", f"Created: {clock():%Y-%m-%d %H:%M}", f"Language: {lang}",
            f"Organised: {_MODE_LABEL[mode]}"]
    if translated and provider_name:
        info.append(f"Translated with: {provider_name}")
    unit = ("sentence", "sentences") if parsed.unit == "sentence" else ("entry", "entries")
    stats = f"{len(entries)} {_plural(len(entries), *unit)} from {parsed.lines_read} {_plural(parsed.lines_read, 'line')}"
    removed = []
    if parsed.duplicates:
        removed.append(f"{parsed.duplicates} {_plural(parsed.duplicates, 'duplicate')}")
    if parsed.noise:
        removed.append(f"{parsed.noise} noise {_plural(parsed.noise, 'line')}")
    if parsed.furigana:
        removed.append(f"{len(parsed.furigana)} furigana {_plural(len(parsed.furigana), 'line')}")
    if removed:
        stats += " (removed " + ", ".join(removed[:-1]) + (" and " if len(removed) > 1 else "") + removed[-1] + ")"
    parts = ["# OCR table", "", "  \n".join(info), "", stats]
    if mode == "vocab":
        parts[-1] = stats + "  \n" + f"{len(words)} {_plural(len(words), 'word')} from {len(entries)} " \
                                       f"{_plural(len(entries), *unit)}"
    parts.append("")
    if parsed.furigana:
        shown = ", ".join(parsed.furigana[:12]) + (f" and {len(parsed.furigana) - 12} more" if len(parsed.furigana) > 12 else "")
        parts += [f"> Left out as furigana (small reading lines above kanji): {shown}. "
                  "Untick \"Ignore furigana\" to keep them.", ""]
    for note in notes:
        parts += [f"> {note}", ""]
    parts += [render_table(columns, rows, right), ""]
    return "\n".join(parts)


def unique_path(folder: Path, stem: str, suffix: str = ".md") -> Path:
    """First free `<stem><suffix>`, `<stem>_2<suffix>`, ... Existing files are never replaced."""
    for n in range(1, 1000):
        candidate = folder / (f"{stem}{suffix}" if n == 1 else f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
    raise CleanerError(f"Could not find a free file name in {folder}.")


def write_new_file(folder: Path, stem: str, content: str) -> Path:
    """Create the file exclusively, so a file that appears between the check and the write is not overwritten."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CleanerError(f"Cannot create the folder {folder}: {exc.strerror or exc}") from exc
    for _ in range(50):
        path = unique_path(folder, stem)
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            return path
        except FileExistsError:
            continue
        except OSError as exc:
            raise CleanerError(f"Could not write {path}: {exc.strerror or exc}") from exc
    raise CleanerError(f"Could not find a free file name in {folder}.")


# --------------------------------------------------------------------------------------------------------------
# translation
# --------------------------------------------------------------------------------------------------------------

def is_english(language: str) -> bool:
    return (language or "").strip().lower().startswith("english")


def build_translation_prompt(items: dict, language: str, words: bool = False) -> str:
    """`items` maps an id to a text. With words=True each value is {"word": ..., "sentence": ...} instead."""
    lang = (language or "").strip()
    src = "automatically detect the language of each entry" if not lang or lang.lower().startswith("auto") \
        else f"the entries are {lang}"
    if words:
        what = ("Each entry is a word with the sentence it was taken from. Translate the WORD into English as it is "
                "used in that sentence, as a short dictionary-style gloss of one to four words, never a translation of "
                "the whole sentence.")
        shape = "The input is a JSON object mapping an id to {\"word\": ..., \"sentence\": ...}. "
    else:
        what = ("Each entry is a separate word, phrase or line of on-screen text: translate it on its own, never "
                "merge, split, reorder, summarise or explain entries.")
        shape = "The input is a JSON object mapping an id to an entry. "
    return (
        "You are a translation engine. Translate every entry below into English; " + src + ". " + what +
        " Add no romanisation, notes or quotation marks. "
        "If it is already English, a name, a number or a code, repeat it unchanged. " + shape +
        "Reply with ONLY a JSON object with exactly the same ids mapping to the English translation. "
        "No markdown, no code fences, no other text.\n\n"
        + json.dumps(items, ensure_ascii=False)
    )


_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```$", re.S)
_NUMBERED_RE = re.compile(r'^\s*"?(\d+)"?\s*[:.)\-]\s*(.+?)\s*$')


def parse_translation_reply(reply: str, ids: list) -> dict:
    """{id: translation} for the ids the reply actually answers. Never invents anything for a missing id."""
    text = (reply or "").strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    found = {}
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    v = str(v)
                if isinstance(v, str) and v.strip():
                    found[str(k)] = " ".join(v.split())
    if not found:
        lb, rb = text.find("["), text.rfind("]")
        if lb != -1 and rb > lb:
            try:
                arr = json.loads(text[lb:rb + 1])
            except ValueError:
                arr = None
            if isinstance(arr, list) and len(arr) == len(ids) and all(isinstance(x, str) and x.strip() for x in arr):
                found = {i: " ".join(x.split()) for i, x in zip(ids, arr)}
    if not found:  # last resort for models that ignore the JSON instruction: lines like "1: text"
        for line in text.splitlines():
            nm = _NUMBERED_RE.match(line)
            if nm:
                found[nm.group(1)] = " ".join(nm.group(2).split())
    return {i: found[i] for i in ids if i in found}


@dataclass
class TranslationOutcome:
    done: int = 0
    failed: int = 0
    providers: list = field(default_factory=list)
    errors: list = field(default_factory=list)


class Translator:
    """Translates entries with the configured provider(s). Text goes to that provider; nothing else is used."""

    MAX_ITEMS = 40
    MAX_CHARS = 3000
    RATE_LIMIT_DELAYS = (3.0, 8.0)   # waits before retrying when every key of every provider said "rate limited"
    MAX_FAILED_BATCHES_IN_A_ROW = 2

    def __init__(self, cfg: dict, provider: str = "auto", caller=call_provider_text, sleep=time.sleep):
        self.cfg = cfg
        self.provider = (provider or "auto").strip() or "auto"
        self.caller = caller
        self.sleep = sleep
        self._client = OCRClient(lambda: cfg)
        self._key_pos = {}

    # which providers
    def chain(self) -> list:
        """Provider ids to try, in order. Local OCR can't translate, so it is never in here."""
        if self.provider != "auto":
            pdef = PROVIDER_BY_ID.get(self.provider)
            if pdef and pdef["kind"] != "local" and self._client.eligible(self.provider, self.cfg):
                return [self.provider]
            return []
        return [pid for pid in self._client.chain(self.cfg) if PROVIDER_BY_ID[pid]["kind"] != "local"]

    @staticmethod
    def available(cfg: dict) -> list:
        """[(provider id, name)] that could translate right now, for the dropdown."""
        client = OCRClient(lambda: cfg)
        return [(p["id"], p["name"]) for p in PROVIDER_BY_ID.values()
                if p["kind"] != "local" and client.eligible(p["id"], cfg)]

    def problem(self):
        if self.chain():
            return None
        if self.provider != "auto":
            return ("The provider you picked for translation is not ready. Add its API key on the OCR Mode tab, "
                    "or choose another provider.")
        return ("Translation needs an AI provider, and Local OCR can't translate. Add a free API key (for example "
                "Google AI Studio) or set up Ollama on the OCR Mode tab, then try again. You can also untick "
                "Translate to get the table without the English column.")

    # asking
    def _ask(self, prompt: str, cancel):
        """(reply text, provider name). Tries every key of every provider; waits and retries on rate limits."""
        errors = []
        for attempt in range(len(self.RATE_LIMIT_DELAYS) + 1):
            rate_limited = False
            for pid in self.chain():
                pdef, pc = PROVIDER_BY_ID[pid], self.cfg["providers"][pid]
                keys = parse_keys(pc.get("keys", "")) or ([""] if not pdef["needs_key"] else [])
                if not keys:
                    continue
                start = self._key_pos.get(pid, 0) % len(keys)
                for off in range(len(keys)):
                    if cancel and cancel():
                        raise CleanerCancelled()
                    i = (start + off) % len(keys)
                    try:
                        reply = self.caller(pdef, pc, keys[i], prompt)
                    except OCRError as exc:
                        errors.append(f"{pdef['name']}: {exc}")
                        if "HTTP 429" in str(exc):
                            rate_limited = True
                        if exc.skip_provider:
                            break
                        continue
                    self._key_pos[pid] = i + 1
                    return reply, pdef["name"]
            if rate_limited and attempt < len(self.RATE_LIMIT_DELAYS):
                self.sleep(self.RATE_LIMIT_DELAYS[attempt])
                continue
            break
        raise CleanerError(" | ".join(dict.fromkeys(errors))[:400] or "No provider answered.")

    def _batches(self, idxs: list, texts: list, contexts: list | None = None):
        batch, chars = [], 0
        for i in idxs:
            n = len(texts[i]) + (len(contexts[i]) if contexts else 0)
            if batch and (len(batch) >= self.MAX_ITEMS or chars + n > self.MAX_CHARS):
                yield batch
                batch, chars = [], 0
            batch.append(i)
            chars += n
        if batch:
            yield batch

    def translate(self, texts: list, language: str = "Auto-detect", progress=None, cancel=None, contexts=None):
        """-> (list of translation or None per text, TranslationOutcome).

        With `contexts` (one sentence per text) each text is a word, translated as used in its sentence."""
        out = [None] * len(texts)
        outcome = TranslationOutcome()
        stack = list(reversed(list(self._batches(list(range(len(texts))), texts, contexts))))
        total = len(texts)
        failed_in_a_row = 0
        while stack:
            if cancel and cancel():
                raise CleanerCancelled()
            batch = stack.pop()
            ids = [str(n) for n in range(1, len(batch) + 1)]
            if contexts:
                items = {k: {"word": texts[i], "sentence": contexts[i]} for k, i in zip(ids, batch)}
            else:
                items = {k: texts[i] for k, i in zip(ids, batch)}
            prompt = build_translation_prompt(items, language, words=bool(contexts))
            try:
                reply, pname = self._ask(prompt, cancel)
            except CleanerError as exc:
                outcome.errors.append(str(exc))
                failed_in_a_row += 1
                outcome.failed += len(batch)
                if failed_in_a_row >= self.MAX_FAILED_BATCHES_IN_A_ROW:
                    for rest in stack:           # stop hammering a provider that keeps failing
                        outcome.failed += len(rest)
                    stack.clear()
                continue
            failed_in_a_row = 0
            if pname not in outcome.providers:
                outcome.providers.append(pname)
            got = parse_translation_reply(reply, ids)
            missing = []
            for k, i in zip(ids, batch):
                if k in got:
                    out[i] = got[k]
                    outcome.done += 1
                else:
                    missing.append(i)
            if missing:
                if len(missing) < len(batch):
                    stack.append(missing)                       # progress was made: ask again for just those
                elif len(missing) > 1:
                    half = len(missing) // 2
                    stack.append(missing[half:])                # no progress: smaller batches
                    stack.append(missing[:half])
                else:
                    outcome.failed += 1                         # a single entry the model will not answer
                    outcome.errors.append("The provider returned no usable translation for some entries.")
            if progress:
                progress(outcome.done + outcome.failed, total, "Translating")
        return out, outcome


# --------------------------------------------------------------------------------------------------------------
# the whole job
# --------------------------------------------------------------------------------------------------------------

@dataclass
class CleanResult:
    path: Path
    markdown: str
    entries: int                 # rows in the table
    lines_read: int
    duplicates: int
    noise: int
    translated: bool = False
    translated_count: int = 0
    failed_count: int = 0
    provider_name: str = ""
    notes: list = field(default_factory=list)
    mode: str = "lines"          # "lines", "sentence" or "vocab"
    furigana: list = field(default_factory=list)
    sentences: int = 0           # in vocab mode: how many sentences the words came from

    def summary(self) -> str:
        if self.mode == "vocab":
            s = (f"{self.entries} {_plural(self.entries, 'word')} from {self.sentences} "
                 f"{_plural(self.sentences, 'sentence')}")
        else:
            unit = ("sentence", "sentences") if self.mode == "sentence" else ("entry", "entries")
            s = f"{self.entries} {_plural(self.entries, *unit)} from {self.lines_read} {_plural(self.lines_read, 'line')}"
            removed = []
            if self.duplicates:
                removed.append(f"{self.duplicates} {_plural(self.duplicates, 'duplicate')}")
            if self.noise:
                removed.append(f"{self.noise} noise {_plural(self.noise, 'line')}")
            if self.furigana:
                removed.append(f"{len(self.furigana)} furigana {_plural(len(self.furigana), 'line')}")
            if removed:
                s += " (" + ", ".join(removed) + " removed)"
        if self.translated:
            s += f", {self.translated_count} translated"
            if self.failed_count:
                s += f", {self.failed_count} could not be translated"
        return s


MODES = ("lines", "sentence", "vocab")


def clean_file(src, *, translate: bool = False, language: str = "Auto-detect", cfg: dict | None = None,
               provider: str = "auto", out_dir=None, progress=None, cancel=None, translator: Translator | None = None,
               clock=datetime.now, mode: str = "sentence", ignore_furigana: bool = True,
               tokenizer=None) -> CleanResult:
    """Read `src`, write `<name>_table.md` next to it (or in out_dir) and return what happened.

    mode: "sentence" (default) one row per sentence, wrapped lines glued back together;
          "vocab" one row per word, each with the sentence it came from;
          "lines" one row per OCR line, as the file has them.
    `tokenizer` is the Japanese word splitter (anything with tokenize() and reading()); None uses SudachiPy.

    Nothing is written if the file has no readable text, if translation is requested but cannot work at all,
    or if `cancel()` turns true. An existing file is never overwritten.
    """
    if mode not in MODES:
        raise CleanerError(f"Unknown mode {mode!r}.")
    src_path = Path(os.path.expanduser(str(src)))
    if progress:
        progress(0, 1, "Reading the file")
    parsed = parse_output(read_source(src_path))
    if not parsed.entries:
        if parsed.lines_read == 0:
            raise CleanerError("The file is empty. There is nothing to clean yet.")
        raise CleanerError("The file has no readable text (every line was empty or only symbols).")

    if mode != "lines":
        if progress:
            progress(0, 1, "Finding sentences")
        parsed = to_sentences(parsed, ignore_furigana=ignore_furigana,
                              get_tokenizer=(lambda: tokenizer) if tokenizer is not None else optional_tokenizer)
        if not parsed.entries:
            raise CleanerError("Nothing was left after the furigana lines were removed. Untick "
                               "\"Ignore furigana\" and try again.")

    words = None
    if mode == "vocab":
        if progress:
            progress(0, 1, "Splitting words")
        try:
            words = extract_words([e.text for e in parsed.entries], language, tokenizer)
        except WordSplitError as exc:
            raise CleanerError(str(exc)) from exc
        if not words:
            raise CleanerError("No words were found. The file only holds symbols, numbers or particles.")
    rows = words if mode == "vocab" else parsed.entries
    label = "words" if mode == "vocab" else "entries"

    notes = []
    translated = False
    pname = ""
    ok = fail = 0
    if translate and is_english(language):
        notes.append("The text is English, so no English column was added.")
    elif translate:
        if len(rows) > MAX_TRANSLATE_ENTRIES:
            raise CleanerError(f"There are {len(rows)} {label}, more than the {MAX_TRANSLATE_ENTRIES} that "
                               "one translation run allows. Clean without translating, or split the file.")
        tr = translator or Translator(cfg if cfg is not None else {}, provider)
        why = tr.problem()
        if why:
            raise CleanerError(why)
        if mode == "vocab":
            result, outcome = tr.translate([w.word for w in rows], language, progress, cancel,
                                           contexts=[w.sentence for w in rows])
        else:
            result, outcome = tr.translate([e.text for e in rows], language, progress, cancel)
        if outcome.done == 0:
            detail = outcome.errors[0] if outcome.errors else "no answer"
            raise CleanerError(f"Translation failed, so nothing was saved: {detail}")
        for row, t in zip(rows, result):
            row.translation = t
        translated, ok, fail = True, outcome.done, outcome.failed
        pname = ", ".join(outcome.providers)
        if fail:
            notes.append(f"{fail} of {len(rows)} {label} could not be translated and are marked "
                         f"{NOT_TRANSLATED}. Run the cleaner again to retry.")
    if cancel and cancel():
        raise CleanerCancelled()

    markdown = build_markdown(parsed.entries, source_name=src_path.name, language=language, translated=translated,
                              with_times=parsed.has_times, parsed=parsed, provider_name=pname, notes=notes,
                              clock=clock, mode=mode, words=words)
    folder = Path(os.path.expanduser(str(out_dir))) if out_dir else src_path.parent
    path = write_new_file(folder, src_path.stem + TABLE_SUFFIX, markdown)
    if progress:
        progress(1, 1, "Done")
    return CleanResult(path=path, markdown=markdown, entries=len(rows), lines_read=parsed.lines_read,
                       duplicates=parsed.duplicates, noise=parsed.noise, translated=translated, translated_count=ok,
                       failed_count=fail, provider_name=pname, notes=notes, mode=mode, furigana=parsed.furigana,
                       sentences=len(parsed.entries))
