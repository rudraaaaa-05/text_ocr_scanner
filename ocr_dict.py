from __future__ import annotations
import argparse
import bz2
import csv
import gzip
import hashlib
import html
import io
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from ocr_nlp import is_han, is_kana, kata_to_hira, optional_tokenizer

SCHEMA_VERSION = 3
KINDS = ("vocab", "grammar", "kanji")
KIND_LABELS = {"vocab": "Vocab", "grammar": "Grammar", "kanji": "Kanji"}
PAGE_SIZE = 50
NOTE_SLOTS = (1, 2)
RECENTS_KEPT = 100
SENTENCE_CAP = 400                 # most example sentences collected for one entry
PROJECT_DIR = Path(__file__).resolve().parent
SEED_FILE = PROJECT_DIR / "data" / "seed_dictionary.json"
GRAMMAR_FILE = PROJECT_DIR / "data" / "grammar_jlpt.csv"
JLPT_ORDER = ("N5", "N4", "N3", "N2", "N1")                  # easiest first; a grammar entry's rank is its position here
JLPT_RANK = {lv: i for i, lv in enumerate(JLPT_ORDER)}
_END = "\U0010ffff"                # sorts after every character: text >= q AND text < q + _END is a prefix match

# --------------------------------------------------------------------------------------------------------------
# languages and sources
# --------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceSpec:
    key: str
    label: str
    url: str
    license: str
    filenames: tuple          # accepted file names in the source folder, best first
    group: str                # sources of one group are imported together: "jmdict", "kanjidic", "tatoeba"
    hint: str = ""


@dataclass(frozen=True)
class LanguageSpec:
    id: str
    name: str
    db_name: str
    kinds: tuple
    sources: tuple
    groups: tuple             # (group, what it provides) in display order


JAPANESE = LanguageSpec(
    id="ja", name="Japanese", db_name="dictionary.sqlite3", kinds=KINDS,
    sources=(
        SourceSpec("jmdict", "JMdict (English glosses)", "http://ftp.edrdg.org/pub/Nihongo/JMdict_e.gz",
                   "CC BY-SA 4.0, EDRDG", ("JMdict_e.gz", "JMdict_e", "JMdict_e.xml", "JMdict_e.xml.gz"), "jmdict"),
        SourceSpec("kanjidic", "KANJIDIC2", "http://www.edrdg.org/kanjidic/kanjidic2.xml.gz",
                   "CC BY-SA 4.0, EDRDG", ("kanjidic2.xml.gz", "kanjidic2.xml"), "kanjidic"),
        SourceSpec("tatoeba_jpn", "Tatoeba Japanese sentences",
                   "https://downloads.tatoeba.org/exports/per_language/jpn/jpn_sentences.tsv.bz2",
                   "CC BY 2.0 FR, Tatoeba contributors",
                   ("jpn_sentences.tsv", "jpn_sentences.tsv.bz2", "jpn_sentences.tsv.gz"), "tatoeba"),
        SourceSpec("tatoeba_eng", "Tatoeba English sentences",
                   "https://downloads.tatoeba.org/exports/per_language/eng/eng_sentences.tsv.bz2",
                   "CC BY 2.0 FR, Tatoeba contributors",
                   ("eng_sentences.tsv", "eng_sentences.tsv.bz2", "eng_sentences.tsv.gz"), "tatoeba"),
        SourceSpec("tatoeba_links", "Tatoeba Japanese-English links",
                   "https://downloads.tatoeba.org/exports/per_language/jpn/jpn-eng_links.tsv.bz2",
                   "CC BY 2.0 FR, Tatoeba contributors",
                   ("jpn-eng_links.tsv", "jpn-eng_links.tsv.bz2", "jpn-eng_links.tsv.gz"), "tatoeba"),
    ),
    groups=(("jmdict", "vocabulary and meanings"), ("kanjidic", "kanji"), ("tatoeba", "example sentences")),
)
LANGUAGES = {JAPANESE.id: JAPANESE}
DEFAULT_LANGUAGE = JAPANESE.id


def source_dir() -> Path:
    """Where downloaded source files are looked for: $OCR_SCANNER_DICT_DIR, else <project>/data/dictionary."""
    env = os.environ.get("OCR_SCANNER_DICT_DIR", "").strip()
    return Path(env).expanduser() if env else PROJECT_DIR / "data" / "dictionary"


def find_sources(lang: LanguageSpec = JAPANESE, directory: Path | None = None) -> dict:
    """{source key: Path or None}: the downloaded files found in the source folder."""
    directory = Path(directory) if directory else source_dir()
    out = {}
    for s in lang.sources:
        out[s.key] = next((directory / n for n in s.filenames if (directory / n).is_file()), None)
    return out


def default_paths(lang: LanguageSpec = JAPANESE):
    """(dictionary file, user file). $OCR_SCANNER_DICT_DB overrides the dictionary file."""
    from ocr_core import config_dir
    base = config_dir()
    env = os.environ.get("OCR_SCANNER_DICT_DB", "").strip()
    db = Path(env).expanduser() if env else base / lang.db_name
    return db, base / "dictionary_user.sqlite3"


# --------------------------------------------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id             INTEGER PRIMARY KEY,
    kind           TEXT NOT NULL,                 -- vocab | grammar | kanji
    level          INTEGER NOT NULL DEFAULT 0,    -- legacy (old JLPT column): kept so old files stay readable, never used
    headword       TEXT NOT NULL,
    key            TEXT NOT NULL,
    reading        TEXT NOT NULL DEFAULT '',
    meaning        TEXT NOT NULL DEFAULT '',      -- all senses, for display
    extra          TEXT NOT NULL DEFAULT '{}',    -- JSON: senses, kanji forms, kana forms, pos, on/kun readings ...
    rank           INTEGER NOT NULL DEFAULT 1,    -- 0 = common word, listed first
    freq           INTEGER NOT NULL DEFAULT 99,   -- JMdict nfNN frequency band, 99 = none
    sort_key       TEXT NOT NULL DEFAULT '',      -- reading in hiragana: alphabetical order
    search_meaning TEXT NOT NULL DEFAULT ''       -- ';gloss;gloss;' lower case, for English search
);
CREATE TABLE IF NOT EXISTS forms (                -- every way to write / read an entry, for exact and prefix search
    entry_id INTEGER NOT NULL,
    text     TEXT NOT NULL,
    role     INTEGER NOT NULL,                    -- 0 = written form (headword), 1 = reading, 2 = romaji (grammar only, found last)
    PRIMARY KEY (text, role, entry_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sentences (id INTEGER PRIMARY KEY, ja TEXT NOT NULL, en TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_entries_browse ON entries(kind, sort_key, headword, id);
CREATE INDEX IF NOT EXISTS ix_entries_common ON entries(kind, rank, sort_key, headword, id);
CREATE INDEX IF NOT EXISTS ix_forms_entry ON forms(entry_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sentences_pair ON sentences(ja, en);
"""

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    kind TEXT NOT NULL, headword TEXT NOT NULL, reading TEXT NOT NULL DEFAULT '', slot INTEGER NOT NULL,
    value TEXT NOT NULL, PRIMARY KEY (kind, headword, reading, slot)
);
CREATE TABLE IF NOT EXISTS recents (
    kind TEXT NOT NULL, headword TEXT NOT NULL, reading TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
    PRIMARY KEY (kind, headword, reading)
);
"""
USER_SCHEMA_VERSION = 2


class DictionaryError(Exception):
    """The dictionary file cannot be used. The message is meant to be shown as is."""


# --------------------------------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------------------------------

POS_NAMES = {
    "n": "noun", "n-adv": "adverbial noun", "n-suf": "noun (suffix)", "n-pref": "noun (prefix)", "n-t": "temporal noun",
    "pn": "pronoun", "adj-i": "i-adjective", "adj-na": "na-adjective", "adj-no": "no-adjective", "adj-f": "prenominal",
    "adj-pn": "pre-noun adjectival", "adv": "adverb", "adv-to": "adverb (to)", "aux": "auxiliary", "aux-v": "auxiliary verb",
    "aux-adj": "auxiliary adjective", "conj": "conjunction", "cop": "copula", "ctr": "counter", "exp": "expression",
    "int": "interjection", "num": "numeric", "pref": "prefix", "suf": "suffix", "prt": "particle", "vs": "suru verb",
    "vs-i": "suru verb (included)", "vs-s": "suru verb (special)", "v1": "Ichidan verb", "v1-s": "Ichidan verb (kureru)",
    "vk": "kuru verb", "vi": "intransitive verb", "vt": "transitive verb", "v5u": "Godan verb (u)", "v5k": "Godan verb (ku)",
    "v5g": "Godan verb (gu)", "v5s": "Godan verb (su)", "v5t": "Godan verb (tsu)", "v5n": "Godan verb (nu)",
    "v5b": "Godan verb (bu)", "v5m": "Godan verb (mu)", "v5r": "Godan verb (ru)", "v5r-i": "Godan verb (ru, irregular)",
    "v5aru": "Godan verb (-aru)", "v5k-s": "Godan verb (iku/yuku)", "v5u-s": "Godan verb (u, special)",
    "vz": "Ichidan verb (zuru)", "unc": "unclassified",
}
ALPHA_ROWS = (("あ", "\u3041", "\u304b"), ("か", "\u304b", "\u3055"), ("さ", "\u3055", "\u305f"), ("た", "\u305f", "\u306a"),
              ("な", "\u306a", "\u306f"), ("は", "\u306f", "\u307e"), ("ま", "\u307e", "\u3084"), ("や", "\u3084", "\u3089"),
              ("ら", "\u3089", "\u308f"), ("わ", "\u308f", "\u30a0"))
OTHER_ROW = "other"                                    # latin letters, digits, symbols: anything that is not kana
ROW_RANGE = {k: (lo, hi) for k, lo, hi in ALPHA_ROWS}
ROW_KEYS = tuple(k for k, _lo, _hi in ALPHA_ROWS) + (OTHER_ROW,)


def norm_key(headword: str) -> str:
    """'〜ざるを得ない' -> 'ざるを得ない': what is looked for in text. Wave dashes, ellipsis and spaces are dropped."""
    return re.sub(r"[〜~～…\.\s・]", "", headword or "").strip()


_STRIP_EDGES = " \t\r\n「」『』（）()[]{}<>“”\"'、。，,.！？!?：:；;・*_`"


def clean_selection(text: str) -> str:
    """Selected text from the Capture output or the table preview -> what to look up.

    Table rows look like '| 食べる | たべる | to eat |': a selection that spans cells is reduced to its first cell that
    holds Japanese. Newlines, bullet marks and surrounding quotes or punctuation are removed."""
    t = (text or "").replace("\u2029", "\n").replace("\u2028", "\n")
    if "|" in t:
        segs = [s.strip() for s in t.split("|") if s.strip()]
        jp = [s for s in segs if any(is_han(c) or is_kana(c) for c in s)]
        t = (jp or segs or [""])[0]
    t = " ".join(t.split())
    return t.strip(_STRIP_EDGES)[:200]


def has_japanese(s: str) -> bool:
    return any(is_han(c) or is_kana(c) for c in s)


def sort_key_for(reading: str, headword: str = "") -> str:
    first = re.split(r"[、,;/]", reading or "")[0].replace(".", "").replace("-", "").strip()
    return kata_to_hira(first or headword or "")


def _gloss_list(extra: dict, meaning: str) -> list:
    senses = extra.get("senses")
    if senses:
        return [g for s in senses for g in s.get("gloss", [])]
    return [g.strip() for g in re.split(r"[;|]", meaning or "") if g.strip()]


def search_meaning_for(extra: dict, meaning: str) -> str:
    glosses = [g.lower().replace(";", ",") for g in _gloss_list(extra, meaning)]
    return ";" + ";".join(glosses) + ";" if glosses else ""


@dataclass
class Entry:
    id: int
    kind: str
    headword: str
    reading: str = ""
    meaning: str = ""
    extra: dict = field(default_factory=dict)
    rank: int = 1
    freq: int = 99
    level: int = 0            # legacy column of old databases. Read, never used.

    @property
    def common(self) -> bool:
        return self.rank == 0

    @property
    def jlpt(self) -> str:
        """'N5' ... 'N1' for grammar points of the JLPT list, otherwise ''."""
        v = str(self.extra.get("jlpt", "")) if self.kind == "grammar" else ""
        return v if v in JLPT_RANK else ""

    @property
    def senses(self) -> list:
        """[{'pos': [...], 'gloss': [...]}]. Old databases and kanji/grammar entries get one sense made from `meaning`."""
        s = self.extra.get("senses")
        if s:
            return s
        pos = [p.strip() for p in str(self.extra.get("pos", "")).split(",") if p.strip()]
        return [{"pos": pos, "gloss": [g.strip() for g in re.split(r"[;|]", self.meaning) if g.strip()]}] if self.meaning else []

    @property
    def readings(self) -> list:
        return list(self.extra.get("kana") or ([self.reading] if self.reading else []))

    @property
    def written_forms(self) -> list:
        return list(self.extra.get("kanji") or [self.headword])

    def short_meaning(self, limit: int = 90) -> str:
        lead = f"{self.jlpt} \u00b7 " if self.jlpt else ""
        senses = self.senses
        if not senses:
            return lead + self.meaning[:limit]
        text = "; ".join(senses[0].get("gloss", [])) or self.meaning
        more = len(senses) - 1
        if len(text) > limit:
            text = text[:limit - 1] + "\u2026"
        return lead + (f"{text}  (+{more} more)" if more > 0 else text)


_COLS_E = "e.id, e.kind, e.headword, e.reading, e.meaning, e.extra, e.rank, e.freq, e.level"


def _row_to_entry(r) -> Entry:
    try:
        extra = json.loads(r[5]) if r[5] else {}
    except ValueError:
        extra = {}
    return Entry(id=r[0], kind=r[1], headword=r[2], reading=r[3], meaning=r[4], extra=extra if isinstance(extra, dict) else {},
                 rank=r[6], freq=r[7], level=r[8])


class Sentence(NamedTuple):
    ja: str
    en: str            # one or several English translations, joined with " / "; may be empty


@dataclass
class SentenceResult:
    items: list
    total: int         # distinct Japanese sentences found (at most SENTENCE_CAP)
    capped: bool       # True when there may be more than `total`


# --------------------------------------------------------------------------------------------------------------
# creating and filling a database (used by ocr_dict.py and for the built-in sample)
# --------------------------------------------------------------------------------------------------------------

def create_schema(con: sqlite3.Connection):
    con.executescript(SCHEMA)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def add_entry(con: sqlite3.Connection, kind: str, headword: str, reading: str = "", meaning: str = "",
              extra: dict | None = None, rank: int = 1, freq: int = 99, forms: list | None = None) -> int:
    """Insert one entry and the forms it can be found by. forms = [(text, role)]; the default is the headword
    (role 0), its normalised key and the reading (role 1, also in hiragana when it is written in katakana)."""
    extra = extra or {}
    cur = con.execute(
        "INSERT INTO entries (kind, headword, key, reading, meaning, extra, rank, freq, sort_key, search_meaning) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (kind, headword, norm_key(headword), reading or "", meaning or "", json.dumps(extra, ensure_ascii=False),
         int(rank), int(freq), sort_key_for(reading, headword), search_meaning_for(extra, meaning)))
    eid = cur.lastrowid
    if forms is None:
        forms = [(headword, 0), (norm_key(headword), 0)] + [(r, 1) for r in re.split(r"[、,]", reading or "")]
    seen = set()
    for text, role in forms:
        text = (text or "").strip()
        variants = [text] + ([kata_to_hira(text)] if role == 1 else [])
        for t in variants:
            if t and (t, role) not in seen:
                seen.add((t, role))
                con.execute("INSERT OR IGNORE INTO forms (entry_id, text, role) VALUES (?,?,?)", (eid, t, role))
    return eid


def load_seed(path: Path = SEED_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_JP_WORD = re.compile(r"^[\u3041-\u3096\u30a1-\u30fa\u30fc\u3005\u3400-\u9fff]+$")      # kana, ー, kanji and nothing else
_ALL_KANA = re.compile(r"^[\u3041-\u3096\u30a1-\u30fa\u30fc]+$")


def grammar_variants(expr: str) -> list:
    """'ちゃいけない・じゃいけない' -> ['ちゃいけない', 'じゃいけない']; '[A]', '(optional)' parts and ellipses are dropped."""
    s = re.sub(r"[\[\uff3b][^\]\uff3d]*[\]\uff3d]", "\u301c", expr)       # [A] marks a gap, [2] a homonym number
    s = re.sub(r"[(\uff08][^)\uff09]*[)\uff09]", "", s)                    # とか（で）: the optional part is not searched for
    s = re.sub(r"[\u30fb\uff65]{2,}|\.{2,}|\u2026+", "\u301c", s)            # ・・・ ... … all mean "something goes here"
    return [v.strip() for v in re.split(r"\s*[/\uff0f\u30fb\uff65;\uff1b&+\u3001]\s*", s) if v.strip()]


def grammar_keys(expr: str) -> list:
    """What to look for in sentences: per variant its longest fixed piece ('は〜より' -> 'より', 'ようが〜ようが' -> 'ようが').
    Pieces of one kana (は, が, に ...) match every sentence, so they are not used."""
    keys: list = []
    for v in grammar_variants(expr):
        pieces = [x for x in re.split(r"[\u301c~\uff5e\u2026\s]+", v) if x and _JP_WORD.match(x)]
        if not pieces:
            continue
        best = max(pieces, key=len)
        if (len(best) >= 2 or any(is_han(c) for c in best)) and best not in keys:
            keys.append(best)
    return keys


def grammar_romaji_forms(romaji: str) -> list:
    s = re.sub(r"\[[^\]]*\]", "", (romaji or "").lower())
    out: list = []
    for v in re.split(r"\s*[/;]\s*", s):
        v = " ".join(re.sub(r"[~\u2026]|\.{2,}", " ", v).split())
        for t in (v, v.replace(" ", "")):
            if t and t not in out:
                out.append(t)
    return out


def parse_grammar_csv(path) -> list:
    """Read the JLPT grammar list (data/grammar_jlpt.csv). One point per row, no header row:
    level (N5..N1), number, expression, romaji, English meaning. Further columns and rows without a valid JLPT level
    (blank lines, a header) are ignored. Returns records in the shape insert_seed() takes for grammar."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = [[c.strip() for c in r] for r in csv.reader(f)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise DictBuildError(f"Grammar file {path}: {exc}") from exc
    recs = []
    for r in rows:
        if len(r) < 5 or r[0].upper() not in JLPT_RANK or not r[2] or not r[4]:
            continue
        expr = " ".join(r[2].split())
        variants = grammar_variants(expr)
        first = variants[0].strip("\u301c~\uff5e\u2026 ") if variants else ""
        if re.search(r"[\u301c~\uff5e\u2026\s]", first):                      # a gap inside (は〜より): no plain reading
            first = ""
        aliases = []
        for t in [expr] + variants + [norm_key(v) for v in variants]:
            if t and t not in aliases:
                aliases.append(t)
        recs.append({"headword": expr, "reading": first if _ALL_KANA.match(first) else "", "meaning": r[4],
                     "jlpt": r[0].upper(), "romaji": r[3], "no": int(r[1]) if r[1].isdigit() else 0,
                     "key": grammar_keys(expr), "aliases": aliases, "rforms": grammar_romaji_forms(r[3])})
    if not recs:
        raise DictBuildError(f"Grammar file {path}: no usable rows. Expected: level (N5-N1), number, expression, romaji, meaning.")
    # the same expression at two levels (こと N4 / N3) or twice in one (そうだ [1] / [2]) must stay two entries
    per_expr, per_level = Counter(x["headword"] for x in recs), Counter((x["headword"], x["jlpt"]) for x in recs)
    running: Counter = Counter()
    for x in recs:
        if per_expr[x["headword"]] > 1:
            running[(x["headword"], x["jlpt"])] += 1
            tag = x["jlpt"] + (f" #{running[(x['headword'], x['jlpt'])]}" if per_level[(x["headword"], x["jlpt"])] > 1 else "")
            x["headword"] = f"{x['headword']} ({tag})"
    return recs


def load_grammar_file(path) -> list:
    """A JLPT grammar CSV (see parse_grammar_csv) or a JSON list / {"grammar": [...]} of objects with a headword."""
    path = Path(path)
    if path.suffix.lower() != ".json":
        return parse_grammar_csv(path)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        recs = data["grammar"] if isinstance(data, dict) else data
        for g in recs:
            g["headword"]
        return recs
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DictBuildError(f"Grammar file {path}: {exc}. Expected a list of objects with a headword.") from exc


def insert_seed(con: sqlite3.Connection, seed: dict, kinds=KINDS, with_sentences: bool = True):
    """Insert the bundled sample (or any JSON of the same shape: see data/seed_dictionary.json)."""
    for kind in kinds:
        for e in seed.get(kind, []):
            extra = {k: v for k, v in e.items() if k not in ("level", "headword", "reading", "meaning")}
            reading = e.get("reading", "")
            if kind == "kanji" and not reading:
                reading = "、".join(x for x in (e.get("onyomi", ""), e.get("kunyomi", "")) if x)
            forms = None
            rank = 0 if kind == "vocab" else 1
            if kind == "grammar":
                aliases, romaji_forms = extra.pop("aliases", []), extra.pop("rforms", [])
                forms = [(e["headword"], 0), (norm_key(e["headword"]), 0)] + [(a, 0) for a in aliases] \
                    + [(e.get("reading", ""), 1)] + [(r, 2) for r in romaji_forms]
                rank = JLPT_RANK.get(e.get("jlpt", ""), 1)
            elif kind == "kanji":
                readings = [r for r in re.split(r"[、,]", reading) if r]
                forms = [(e["headword"], 0)] + [(r, 1) for r in readings] + \
                        [(r.split(".")[0], 1) for r in readings if "." in r] + \
                        [(r.replace(".", ""), 1) for r in readings if "." in r]
            elif kind == "vocab":
                gl = [g.strip() for g in re.split(r"[;|]", e.get("meaning", "")) if g.strip()]
                extra["senses"] = [{"pos": [p.strip() for p in str(e.get("pos", "")).split(",") if p.strip()], "gloss": gl}]
                extra["kanji"] = [e["headword"]]
                extra["kana"] = [reading] if reading else []
            add_entry(con, kind, e["headword"], reading, e.get("meaning", ""), extra, rank=rank, forms=forms)
    if with_sentences:
        con.executemany("INSERT OR IGNORE INTO sentences (ja, en) VALUES (?, ?)",
                        [(s["ja"], s.get("en", "")) for s in seed.get("sentences", [])])


def finish_database(con: sqlite3.Connection) -> bool:
    """Create the indexes (after the bulk inserts, which is faster) and the sentence search index.
    Returns True when the full text index for sentences could be built."""
    con.executescript(INDEXES)
    return build_sentence_index(con)


def build_sentence_index(con: sqlite3.Connection) -> bool:
    """FTS5 with the trigram tokenizer finds any 3+ character substring of a Japanese sentence quickly. Older SQLite
    builds do not have it; lookups then use a plain scan (slower, same results)."""
    con.execute("DROP TABLE IF EXISTS sentences_fts")
    try:
        con.execute("CREATE VIRTUAL TABLE sentences_fts USING fts5(ja, tokenize='trigram')")
    except sqlite3.OperationalError:
        return False
    con.execute("INSERT INTO sentences_fts (rowid, ja) SELECT id, ja FROM sentences")
    return True


def set_meta(con: sqlite3.Connection, key: str, value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False)
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def read_meta(con: sqlite3.Connection) -> dict:
    out = {}
    for k, v in con.execute("SELECT key, value FROM meta"):
        try:
            out[k] = json.loads(v) if k in ("sources", "counts", "skipped") else v
        except ValueError:
            out[k] = v
    return out


def compute_counts(con: sqlite3.Connection) -> dict:
    c = {k: n for k, n in con.execute("SELECT kind, COUNT(*) FROM entries GROUP BY kind")}
    return {"vocab": c.get("vocab", 0), "grammar": c.get("grammar", 0), "kanji": c.get("kanji", 0),
            "sentences": con.execute("SELECT COUNT(*) FROM sentences").fetchone()[0]}


def build_sample_database(con: sqlite3.Connection):
    """The small bundled sample (a few dozen entries). Marked source='sample' so the UI never presents it as the
    full dictionary."""
    create_schema(con)
    insert_seed(con, load_seed())
    finish_database(con)
    set_meta(con, "schema_version", str(SCHEMA_VERSION))
    set_meta(con, "dictionary_id", JAPANESE.id)
    set_meta(con, "dictionary_name", JAPANESE.name)
    set_meta(con, "source", "sample")
    set_meta(con, "imported_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    set_meta(con, "sources", {})
    set_meta(con, "counts", compute_counts(con))
    con.commit()


# --------------------------------------------------------------------------------------------------------------
# status: is there a database, is it valid, is it complete, is it current
# --------------------------------------------------------------------------------------------------------------

@dataclass
class DictStatus:
    state: str                 # missing | invalid | sample | partial | stale | ready | upgrade
    path: Path
    message: str               # what to tell the user
    command: str = ""          # exact command to run, when there is something to do
    info: dict = field(default_factory=dict)
    using_builtin_sample: bool = False

    @property
    def usable(self) -> bool:
        return self.state in ("sample", "partial", "stale", "ready", "upgrade")

    @property
    def complete(self) -> bool:
        return self.state in ("ready", "stale")


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)


def build_command(lang: LanguageSpec = JAPANESE, found: dict | None = None) -> str:
    return "python ocr_dict.py"          # detects the files in the source folder by itself


def full_build_command(lang: LanguageSpec = JAPANESE, directory: Path | None = None) -> str:
    d = Path(directory) if directory else source_dir()
    try:
        d = d.relative_to(PROJECT_DIR)
    except ValueError:
        pass
    flags = {"jmdict": "--jmdict", "kanjidic": "--kanjidic", "tatoeba_jpn": "--tatoeba-jpn",
             "tatoeba_eng": "--tatoeba-eng", "tatoeba_links": "--tatoeba-links"}
    parts = ["python ocr_dict.py"]
    for s in lang.sources:
        parts.append(f"{flags[s.key]} {(d / s.filenames[0]).as_posix()}")
    return " \\\n    ".join(parts)


def _missing_groups(lang: LanguageSpec, sources: dict) -> list:
    have = set()
    for s in lang.sources:
        if s.key in sources:
            have.add(s.group)
    return [g for g, _what in lang.groups if g not in have]


def inspect_db(path, lang: LanguageSpec = JAPANESE, directory: Path | None = None) -> DictStatus:
    """Look at the dictionary file and say what it is. Opens it read-only, never changes it, reads no entries."""
    path = Path(path)
    found = find_sources(lang, directory)
    present = [k for k, p in found.items() if p]
    cmd = build_command(lang, found)
    folder = Path(directory) if directory else source_dir()
    if not path.exists():
        if len(present) == len(lang.sources):
            msg = (f"The full {lang.name} dictionary has not been built yet, but the downloaded files are in {folder}. "
                   f"Build it with:  {cmd}")
        elif present:
            msg = (f"The full {lang.name} dictionary has not been built yet. Some source files are in {folder}; "
                   f"missing: {', '.join(k for k in found if not found[k])}. Get the rest with:  python ocr_dict.py download   "
                   f"then run:  {cmd}")
        else:
            msg = (f"The full {lang.name} dictionary is not installed. Get the source files (into {folder}) with:  "
                   f"python ocr_dict.py download   then run:  {cmd}")
        return DictStatus("missing", path, msg, cmd)
    try:
        con = _connect_ro(path)
    except sqlite3.Error as exc:
        return DictStatus("invalid", path, f"The dictionary file {path} cannot be opened ({exc}). Rebuild it with:  {cmd}", cmd)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if not {"entries", "meta", "sentences"} <= tables:
            raise DictionaryError("it is not a dictionary file (tables are missing)")
        con.execute("SELECT 1 FROM entries LIMIT 1").fetchall()
        meta = read_meta(con)
        version = int(meta.get("schema_version", 1))
        counts = meta.get("counts") or compute_counts(con)
        has_fts = "sentences_fts" in tables
    except (sqlite3.Error, DictionaryError, ValueError) as exc:
        return DictStatus("invalid", path, f"The dictionary file {path} is not valid ({exc}). Rebuild it with:  {cmd}", cmd)
    finally:
        con.close()

    sources = meta.get("sources") or {}
    info = {"counts": counts, "sources": sources, "imported_at": meta.get("imported_at", ""), "schema_version": version,
            "name": meta.get("dictionary_name", lang.name), "fts": has_fts, "path": str(path)}
    if version > SCHEMA_VERSION:
        return DictStatus("invalid", path, f"The dictionary file was made by a newer version of this app (schema {version}). "
                          f"Update the app, or rebuild it with:  {cmd}", cmd, info)
    if version < SCHEMA_VERSION or "forms" not in tables:
        return DictStatus("upgrade", path, "The dictionary file is from an older version and will be upgraded when opened "
                          "(a backup copy is kept).", cmd, info)
    if meta.get("source") == "sample":
        return DictStatus("sample", path, f"This is only the small built-in sample ({counts.get('vocab', 0)} words). "
                          f"Install the full dictionary: python ocr_dict.py download (into {folder}), "
                          f"then run:  {cmd}", cmd, info)
    missing = _missing_groups(lang, sources)
    if missing:
        names = {g: what for g, what in lang.groups}
        what = ", ".join(names[g] for g in missing)
        return DictStatus("partial", path, f"Imported, but without: {what}. Add the missing source files to {folder} and run:  {cmd}",
                          cmd, info)
    # complete: is it current? Compare with the files on disk.
    changed, newer = [], []
    for s in lang.sources:
        rec, disk = sources.get(s.key), found.get(s.key)
        if disk is None:
            continue
        try:
            size = disk.stat().st_size
        except OSError:
            continue
        if rec and rec.get("size") not in (None, size):
            changed.append(s.key)
    if changed:
        return DictStatus("stale", path, f"The downloaded files changed since the last import ({', '.join(changed)}). "
                          f"Update with:  {cmd}", cmd, info)
    return DictStatus("ready", path, "", "", info)


# --------------------------------------------------------------------------------------------------------------
# upgrading old files (schema 1 had no forms table, no sort key, no sentence index)
# --------------------------------------------------------------------------------------------------------------

def upgrade_db(path) -> list:
    """Bring an older dictionary file to the current schema in place. A copy of the old file is kept next to it
    (<name>.bak-v<old>). Returns the list of steps done. Raises DictionaryError when it cannot be done."""
    path = Path(path)
    steps = []
    con = sqlite3.connect(path)
    try:
        meta = {}
        try:
            meta = read_meta(con)
        except sqlite3.Error:
            pass
        old = int(meta.get("schema_version", 1))
        if old >= SCHEMA_VERSION and "forms" in {r[0] for r in con.execute("SELECT name FROM sqlite_master")}:
            return steps
        backup = path.with_name(f"{path.name}.bak-v{old}")
        con.close()
        try:
            shutil.copy2(path, backup)
        except OSError as exc:
            raise DictionaryError(f"could not make a backup copy before upgrading ({exc})") from exc
        steps.append(f"backup {backup.name}")
        con = sqlite3.connect(path)
        cols = {r[1] for r in con.execute("PRAGMA table_info(entries)")}
        for name, ddl in (("level", "INTEGER NOT NULL DEFAULT 0"), ("rank", "INTEGER NOT NULL DEFAULT 1"),
                          ("freq", "INTEGER NOT NULL DEFAULT 99"), ("sort_key", "TEXT NOT NULL DEFAULT ''"),
                          ("search_meaning", "TEXT NOT NULL DEFAULT ''"), ("key", "TEXT NOT NULL DEFAULT ''")):
            if name not in cols:
                con.execute(f"ALTER TABLE entries ADD COLUMN {name} {ddl}")
                steps.append(f"column {name}")
        con.executescript(SCHEMA)           # adds forms, and meta/sentences when missing
        rows = con.execute("SELECT id, kind, headword, reading, meaning, extra FROM entries").fetchall()
        con.execute("DELETE FROM forms")
        for eid, kind, hw, reading, meaning, extra_s in rows:
            try:
                extra = json.loads(extra_s) if extra_s else {}
            except ValueError:
                extra = {}
            con.execute("UPDATE entries SET key=?, sort_key=?, search_meaning=? WHERE id=?",
                        (norm_key(hw), sort_key_for(reading, hw), search_meaning_for(extra, meaning), eid))
            forms = [(hw, 0), (norm_key(hw), 0)] + [(r, 1) for r in re.split(r"[、,]", reading or "")]
            if kind == "kanji":
                forms += [(r.split(".")[0], 1) for r in re.split(r"[、,]", reading or "") if "." in r]
                forms += [(r.replace(".", ""), 1) for r in re.split(r"[、,]", reading or "") if "." in r]
            seen = set()
            for text, role in forms:
                for t in ([text] + ([kata_to_hira(text)] if role == 1 else [])):
                    if t and (t, role) not in seen:
                        seen.add((t, role))
                        con.execute("INSERT OR IGNORE INTO forms (entry_id, text, role) VALUES (?,?,?)", (eid, t, role))
        steps.append("forms")
        # old files could hold the same pair twice; the unique index needs them gone
        con.execute("DELETE FROM sentences WHERE id NOT IN (SELECT MIN(id) FROM sentences GROUP BY ja, en)")
        finish_database(con)
        steps.append("indexes")
        set_meta(con, "schema_version", str(SCHEMA_VERSION))
        if "source" not in meta:
            set_meta(con, "source", "imported")
        set_meta(con, "dictionary_id", meta.get("dictionary_id", JAPANESE.id))
        set_meta(con, "dictionary_name", meta.get("dictionary_name", JAPANESE.name))
        set_meta(con, "counts", compute_counts(con))
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.commit()
        return steps
    except sqlite3.Error as exc:
        con.rollback()
        raise DictionaryError(f"upgrade failed ({exc}); the original file was copied to {path.name}.bak-v*") from exc
    finally:
        con.close()


# --------------------------------------------------------------------------------------------------------------
# the dictionary
# --------------------------------------------------------------------------------------------------------------

class Dictionary:
    """Read access to the dictionary plus the user's notes and recents. Safe to use from the GUI thread."""

    def __init__(self, path=None, user_path=None, tokenizer=None, *, _memory: sqlite3.Connection | None = None):
        if user_path is None or (path is None and _memory is None):
            dp, up = default_paths()
            path = path or dp
            user_path = user_path or up
        self.builtin_sample = _memory is not None
        self.path = Path(path) if path else None
        self.user_path = Path(user_path)
        self._lock = threading.RLock()
        if _memory is not None:
            self.con = _memory
        else:
            if not self.path.exists():
                raise DictionaryError(f"The dictionary file {self.path} does not exist.")
            try:
                self.con = _connect_ro(self.path)
                self.con.execute("SELECT 1 FROM entries LIMIT 1").fetchall()
                self.con.execute("SELECT 1 FROM forms LIMIT 1").fetchall()
            except sqlite3.Error as exc:
                raise DictionaryError(f"The dictionary file {self.path} cannot be used ({exc}).") from exc
        self._meta = read_meta(self.con)
        self._has_fts = self.con.execute("SELECT 1 FROM sqlite_master WHERE name='sentences_fts'").fetchone() is not None
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        self.ucon = sqlite3.connect(self.user_path, check_same_thread=False)
        _init_user_db(self.ucon)
        self._tokenizer = tokenizer
        self._tokenizer_tried = tokenizer is not None

    @classmethod
    def builtin(cls, user_path=None, tokenizer=None) -> "Dictionary":
        """The bundled sample in memory. Used while no real dictionary file is installed; the UI says so."""
        con = sqlite3.connect(":memory:", check_same_thread=False)
        build_sample_database(con)
        return cls(None, user_path or default_paths()[1], tokenizer, _memory=con)

    def close(self):
        with self._lock:
            for c in (self.con, self.ucon):
                try:
                    c.close()
                except sqlite3.Error:
                    pass

    def _q(self, sql: str, args=()):
        with self._lock:
            return self.con.execute(sql, args).fetchall()

    # -- information
    @property
    def meta(self) -> dict:
        return self._meta

    @property
    def is_sample(self) -> bool:
        return self._meta.get("source") == "sample"

    @property
    def has_sentence_index(self) -> bool:
        return self._has_fts

    def counts(self) -> dict:
        c = self._meta.get("counts")
        return c if isinstance(c, dict) and c else compute_counts(self.con)

    # -- single entries
    def get(self, entry_id: int):
        rows = self._q(f"SELECT {_COLS_E} FROM entries e WHERE e.id=?", (entry_id,))
        return _row_to_entry(rows[0]) if rows else None

    def find(self, kind: str, headword: str, reading: str = ""):
        rows = self._q(f"SELECT {_COLS_E} FROM entries e WHERE e.kind=? AND e.headword=? AND e.reading=? ORDER BY e.id LIMIT 1",
                       (kind, headword, reading))
        return _row_to_entry(rows[0]) if rows else None

    # -- alphabetical browsing
    @staticmethod
    def _browse_where(kind: str, row: str, common: bool, jlpt: str = ""):
        where, args = ["e.kind = ?"], [kind]
        if common:
            where.append("e.rank = 0")
        if jlpt:
            if jlpt not in JLPT_RANK:
                raise ValueError(f"unknown JLPT level {jlpt!r}")
            where.append("e.rank = ?")
            args.append(JLPT_RANK[jlpt])
        if row == OTHER_ROW:
            where.append("(e.sort_key < ? OR e.sort_key >= ?)")
            args += ["\u3041", "\u30a0"]
        elif row:
            if row not in ROW_RANGE:
                raise ValueError(f"unknown row {row!r}")
            lo, hi = ROW_RANGE[row]
            where.append("e.sort_key >= ? AND e.sort_key < ?")
            args += [lo, hi]
        return " AND ".join(where), args

    def browse(self, kind: str, row: str = "", common: bool = False, offset: int = 0, limit: int = PAGE_SIZE,
               jlpt: str = "") -> list:
        """Entries of one kind in kana order (readings, then headword). row = one of ROW_KEYS to show one kana row;
        common=True only common words (vocab); jlpt='N4' only grammar of that level."""
        where, args = self._browse_where(kind, row, common, jlpt)
        rows = self._q(f"SELECT {_COLS_E} FROM entries e WHERE {where} ORDER BY e.sort_key, e.headword, e.id LIMIT ? OFFSET ?",
                       args + [max(1, limit), max(0, offset)])
        return [_row_to_entry(r) for r in rows]

    def browse_count(self, kind: str, row: str = "", common: bool = False, jlpt: str = "") -> int:
        where, args = self._browse_where(kind, row, common, jlpt)
        return self._q(f"SELECT COUNT(*) FROM entries e WHERE {where}", args)[0][0]

    # -- searching
    def _search_parts(self, q: str, kind: str | None):
        h = kata_to_hira(q)
        args = {"q": q, "qe": q + _END, "h": h, "he": h + _END, "ql": q.lower(), "qle": q.lower() + _END}
        sub = ("SELECT entry_id AS id, CASE WHEN role = 2 THEN (CASE WHEN text = :ql THEN 3 ELSE 6 END) "
               "WHEN role = 0 AND text = :q THEN 0 WHEN text = :q OR text = :h THEN 1 ELSE 2 END AS tier "
               "FROM forms WHERE (text >= :q AND text < :qe) OR (text >= :h AND text < :he) "
               "OR (role = 2 AND text >= :ql AND text < :qle)")
        if not has_japanese(q):          # English meaning search: whole gloss first, then part of a gloss
            sub += (" UNION ALL SELECT id, CASE WHEN instr(search_meaning, ';' || :ql || ';') > 0 THEN 3 ELSE 4 END "
                    "FROM entries WHERE instr(search_meaning, :ql) > 0")
        where = ""
        if kind:
            where = "WHERE e.kind = :kind"
            args["kind"] = kind
        return sub, where, args

    def search(self, query: str, kind: str | None = None, limit: int = PAGE_SIZE, offset: int = 0) -> list:
        """Headword, any written form, reading (hiragana or katakana) or English meaning.

        Order: 1 exact headword, 2 exact reading, 3 headword/reading starts with the query, 4 a whole English gloss
        equals it, 5 the query is part of a gloss. Inside each group vocabulary comes before grammar and kanji, common
        words before uncommon ones, then by frequency band, then alphabetically (reading, headword, id), which keeps
        pages stable."""
        q = (query or "").strip()
        if not q:
            return []
        sub, where, args = self._search_parts(q, kind)       # CROSS JOIN below: start from the small candidate set, not from all entries
        args.update(limit=max(1, limit), offset=max(0, offset))
        sql = (f"SELECT {_COLS_E}, MIN(c.tier) AS tier FROM ({sub}) c CROSS JOIN entries e ON e.id = c.id {where} "
               "GROUP BY e.id ORDER BY tier, CASE e.kind WHEN 'vocab' THEN 0 WHEN 'grammar' THEN 1 ELSE 2 END, "
               "e.rank, e.freq, e.sort_key, e.headword, e.id LIMIT :limit OFFSET :offset")
        return [_row_to_entry(r) for r in self._q(sql, args)]

    def search_count(self, query: str, kind: str | None = None) -> int:
        q = (query or "").strip()
        if not q:
            return 0
        sub, where, args = self._search_parts(q, kind)
        return self._q(f"SELECT COUNT(DISTINCT e.id) FROM ({sub}) c CROSS JOIN entries e ON e.id = c.id {where}", args)[0][0]

    def _exact(self, text: str, whole: bool, limit: int = 20) -> list:
        """Entries written exactly `text` (or, for the whole selection, also read exactly `text`)."""
        h = kata_to_hira(text)
        role = "AND f.role < 2" if whole else "AND f.role = 0"
        sql = (f"SELECT {_COLS_E} FROM forms f JOIN entries e ON e.id = f.entry_id WHERE f.text IN (?, ?) {role} "
               "GROUP BY e.id ORDER BY MIN(f.role), CASE e.kind WHEN 'vocab' THEN 0 WHEN 'kanji' THEN 1 ELSE 2 END, "
               "e.rank, e.freq, e.id LIMIT ?")
        return [_row_to_entry(r) for r in self._q(sql, (text, h, limit))]

    def _get_tokenizer(self):
        if not self._tokenizer_tried:
            self._tokenizer_tried = True
            try:
                self._tokenizer = optional_tokenizer()
            except Exception:  # noqa: BLE001  a broken optional dependency must not break lookups
                self._tokenizer = None
        return self._tokenizer

    def lookup(self, text: str, limit: int = 40) -> list:
        """Entries for text the user selected on screen.

        1. an exact hit on the whole selection (written form or reading)
        2. the dictionary form of each word, when SudachiPy is installed (食べました -> 食べる)
        3. the longest dictionary words found inside the selection, scanning left to right"""
        t = clean_selection(text)
        if not t:
            return []
        out, seen = [], set()

        def add(entries):
            for e in entries:
                if e.id not in seen:
                    seen.add(e.id)
                    out.append(e)

        add(self._exact(t, whole=True))
        if out:
            return out[:limit]
        tok = self._get_tokenizer()
        if tok is not None:
            try:
                for token in tok.tokenize(t):
                    if token.pos and token.pos[0] in ("助詞", "助動詞", "補助記号", "空白", "記号"):
                        continue
                    for form in dict.fromkeys((token.lemma, token.surface)):
                        if form:
                            add(self._exact(form, whole=False))
            except Exception:  # noqa: BLE001
                pass
            if out:
                return out[:limit]
        n, i = len(t), 0
        while i < n and len(out) < limit:
            hit = False
            for j in range(min(n, i + 10), i, -1):
                span = t[i:j]
                if j - i == 1 and not is_han(span):
                    continue            # single kana would match every particle
                found = self._exact(span, whole=False, limit=5)
                if not found and j - i >= 2:
                    # no SudachiPy: guess the dictionary form of a stem (食べ -> 食べる, 美味し -> 美味しい)
                    for tail in ("る", "い"):
                        found = [e for e in self._exact(span + tail, whole=False, limit=5) if e.kind == "vocab"]
                        if found:
                            break
                if found:
                    add(found)
                    i, hit = j, True
                    break
            if not hit:
                i += 1
        return out[:limit]

    # -- example sentences
    def sentence_patterns(self, e: Entry) -> list:
        """[(text, score)]: what to look for in sentences. 0 = the written form, 1 = the reading, 2 = a stem of the
        written form, 3 = a stem of the reading (a verb or i-adjective appears conjugated)."""
        pats: dict = {}

        def put(text: str, score: int):
            if text and (len(text) >= 2 or any(is_han(c) for c in text)):
                pats[text] = min(score, pats.get(text, 9))

        if e.kind == "grammar":
            for k in e.extra.get("key") or [norm_key(e.headword)]:
                put(k, 0)
        elif e.kind == "kanji":
            put(e.headword, 0)
        else:
            first = ""
            for s in e.senses:
                if s.get("pos"):
                    first = s["pos"][0]
                    break
            inflects = first.startswith(("verb", "v1", "v5", "vk", "vz")) or first in ("adjective-i", "adj-i")
            for w in e.written_forms:
                put(w, 0)
                if inflects and len(w) > 1:
                    put(w[:-1], 2)
            for r in e.readings:
                put(r, 0 if r == e.headword else 1)
                if inflects and len(r) > 1:
                    put(r[:-1], 3)
        return sorted(pats.items(), key=lambda kv: (kv[1], -len(kv[0])))

    def _sentence_ids(self, pattern: str, cap: int) -> list:
        if self._has_fts and len(pattern) >= 3:
            phrase = '"' + pattern.replace('"', '""') + '"'
            return [r[0] for r in self._q("SELECT rowid FROM sentences_fts WHERE sentences_fts MATCH ? LIMIT ?", (phrase, cap))]
        return [r[0] for r in self._q("SELECT id FROM sentences WHERE instr(ja, ?) > 0 LIMIT ?", (pattern, cap))]

    def sentences_for(self, e: Entry, limit: int = 5, offset: int = 0) -> SentenceResult:
        """Example sentences for an entry, best first: sentences that contain the exact written form, then the
        reading, then conjugated stems; shorter sentences first inside a group. A Japanese sentence with several
        English translations appears once with the translations joined. Pair duplicates are removed."""
        best: dict = {}                       # sentence id -> best pattern score
        capped = False
        for pat, score in self.sentence_patterns(e):
            ids = self._sentence_ids(pat, SENTENCE_CAP)
            capped = capped or len(ids) >= SENTENCE_CAP
            for i in ids:
                if i not in best or score < best[i]:
                    best[i] = score
        if not best:
            return SentenceResult([], 0, False)
        rows = []
        ids = list(best)
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            rows += self._q(f"SELECT id, ja, en FROM sentences WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        grouped: dict = {}                    # ja -> (score, id, [en...])
        for sid, ja, en in rows:
            sc = best[sid]
            cur = grouped.get(ja)
            if cur is None:
                grouped[ja] = [sc, sid, [en] if en else []]
            else:
                cur[0] = min(cur[0], sc)
                cur[1] = min(cur[1], sid)
                if en and en not in cur[2]:
                    cur[2].append(en)
        ordered = sorted(grouped.items(), key=lambda kv: (kv[1][0], len(kv[0]), kv[1][1]))
        items = [Sentence(ja, " / ".join(v[2])) for ja, v in ordered]
        return SentenceResult(items[offset:offset + limit], len(items), capped or len(items) >= SENTENCE_CAP)

    # -- the user's own notes and recent entries
    def get_notes(self, e: Entry) -> dict:
        with self._lock:
            rows = self.ucon.execute("SELECT slot, value, reading FROM notes WHERE kind=? AND headword=? AND reading IN (?, '')",
                                     (e.kind, e.headword, e.reading)).fetchall()
        out = {}
        for slot, value, reading in sorted(rows, key=lambda r: r[2] != ""):   # exact reading overrides a legacy note
            out[slot] = value
        return out

    def set_note(self, e: Entry, slot: int, value: str):
        if slot not in NOTE_SLOTS:
            raise ValueError(f"note slot must be one of {NOTE_SLOTS}")
        value = (value or "").strip()
        with self._lock:
            if value:
                self.ucon.execute("INSERT OR REPLACE INTO notes (kind, headword, reading, slot, value) VALUES (?,?,?,?,?)",
                                  (e.kind, e.headword, e.reading, slot, value))
            else:
                self.ucon.execute("DELETE FROM notes WHERE kind=? AND headword=? AND reading=? AND slot=?",
                                  (e.kind, e.headword, e.reading, slot))
            self.ucon.commit()

    def add_recent(self, e: Entry):
        with self._lock:
            self.ucon.execute("INSERT OR REPLACE INTO recents (kind, headword, reading, ts) VALUES (?,?,?,?)",
                              (e.kind, e.headword, e.reading, time.time()))
            self.ucon.execute("DELETE FROM recents WHERE rowid NOT IN (SELECT rowid FROM recents ORDER BY ts DESC LIMIT ?)",
                              (RECENTS_KEPT,))
            self.ucon.commit()

    def recents(self, limit: int = 40) -> list:
        """Recently opened entries as Entry objects, newest first. Entries that are not in the current dictionary
        file any more are left out."""
        with self._lock:
            rows = self.ucon.execute("SELECT kind, headword, reading FROM recents ORDER BY ts DESC, rowid DESC LIMIT ?",
                                     (limit * 2,)).fetchall()
        out = []
        for kind, hw, reading in rows:
            e = self.find(kind, hw, reading)
            if e is not None:
                out.append(e)
            if len(out) >= limit:
                break
        return out

    def clear_recents(self):
        with self._lock:
            self.ucon.execute("DELETE FROM recents")
            self.ucon.commit()


def _init_user_db(con: sqlite3.Connection):
    """Create or upgrade the user file. Version 1 had notes keyed by (kind, headword, slot); notes are now also keyed
    by reading, because JMdict has many entries with the same headword. Old notes are kept (reading '')."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(notes)")]
    if cols and "reading" not in cols:
        con.execute("BEGIN")
        try:
            con.execute("ALTER TABLE notes RENAME TO notes_v1")
            con.execute("CREATE TABLE notes (kind TEXT NOT NULL, headword TEXT NOT NULL, reading TEXT NOT NULL DEFAULT '', "
                        "slot INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY (kind, headword, reading, slot))")
            con.execute("INSERT INTO notes (kind, headword, reading, slot, value) SELECT kind, headword, '', slot, value FROM notes_v1")
            con.execute("DROP TABLE notes_v1")
            con.execute("COMMIT")
        except sqlite3.Error:
            con.execute("ROLLBACK")
            raise
    con.executescript(USER_SCHEMA)
    con.execute(f"PRAGMA user_version = {USER_SCHEMA_VERSION}")
    con.commit()


def load_dictionary(lang: LanguageSpec = JAPANESE, path=None, user_path=None, tokenizer=None, directory=None):
    """(Dictionary, DictStatus). Never raises. When the real file is missing or unusable the bundled sample is
    opened instead and the status says so (status.using_builtin_sample, state 'missing' or 'invalid')."""
    if path is None or user_path is None:
        dp, up = default_paths(lang)
        path = path or dp
        user_path = user_path or up
    st = inspect_db(path, lang, directory)
    if st.state == "upgrade":
        try:
            steps = upgrade_db(path)
            st = inspect_db(path, lang, directory)
            st.info["upgraded"] = steps
        except DictionaryError as exc:
            st = DictStatus("invalid", Path(path), f"The dictionary file {path} could not be upgraded: {exc}", st.command, st.info)
    if st.usable:
        try:
            return Dictionary(path, user_path, tokenizer), st
        except DictionaryError as exc:
            st = DictStatus("invalid", Path(path), str(exc) + f" Rebuild it with:  {st.command or build_command(lang)}",
                            st.command or build_command(lang), st.info)
    st.using_builtin_sample = True
    return Dictionary.builtin(user_path, tokenizer), st


# --------------------------------------------------------------------------------------------------------------
# back / forward
# --------------------------------------------------------------------------------------------------------------

class History:
    """Browser style navigation. A page is any hashable value."""

    def __init__(self):
        self._pages: list = []
        self._pos = -1

    @property
    def current(self):
        return self._pages[self._pos] if 0 <= self._pos < len(self._pages) else None

    @property
    def can_back(self) -> bool:
        return self._pos > 0

    @property
    def can_forward(self) -> bool:
        return 0 <= self._pos < len(self._pages) - 1

    def push(self, page):
        """Go to a new page. Anything that was forward of the current page is dropped. Going to the page you are
        already on does nothing."""
        if self.current == page:
            return
        del self._pages[self._pos + 1:]
        self._pages.append(page)
        self._pos = len(self._pages) - 1

    def replace(self, page):
        """Change the current page without adding a step (paging inside a list, selecting an entry)."""
        if self._pos < 0:
            self.push(page)
        else:
            self._pages[self._pos] = page

    def back(self):
        if self.can_back:
            self._pos -= 1
        return self.current

    def forward(self):
        if self.can_forward:
            self._pos += 1
        return self.current


# --------------------------------------------------------------------------------------------------------------
# what the Dictionary tab shows (view state, pages, history, HTML for an entry)
# --------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Page:
    mode: str = "home"               # home | browse | results | lookup
    kind: str | None = None          # vocab | grammar | kanji, or None for all kinds
    query: str = ""                  # search text or looked up text
    row: str = ""                    # kana row for browse ("" = all)
    common: bool = False             # browse: common words only
    number: int = 0                  # page number, 0 based
    sel: int | None = None           # id of the selected entry
    jlpt: str = ""                   # browse grammar: one JLPT level ("" = all)


@dataclass
class View:
    page: Page
    title: str = ""
    entries: list = field(default_factory=list)
    total: int = 0
    pages: int = 1
    selected: object = None          # Entry or None
    message: str = ""                # shown instead of the list when it is empty
    recents: list = field(default_factory=list)

    @property
    def is_home(self) -> bool:
        return self.page.mode == "home"

    @property
    def selected_index(self) -> int:
        if self.selected is None:
            return -1
        for i, e in enumerate(self.entries):
            if e.id == self.selected.id:
                return i
        return -1


class BrowserState:
    """Navigation state for one open dictionary."""

    def __init__(self, dic: Dictionary, page_size: int = PAGE_SIZE):
        self.dic = dic
        self.page_size = max(1, page_size)
        self.history = History()
        self.history.push(Page())

    # -- navigation (each returns the View to draw)
    @property
    def page(self) -> Page:
        return self.history.current or Page()

    @property
    def scope(self):
        """The kind a search typed now is limited to, or None for all kinds."""
        p = self.page
        return p.kind if p.mode in ("browse", "results") else None

    def _go(self, page: Page) -> View:
        self.history.push(page)
        return self.view()

    def home(self) -> View:
        return self._go(Page())

    def browse(self, kind: str, row: str = "", common: bool = False, jlpt: str = "") -> View:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if jlpt and jlpt not in JLPT_RANK:
            raise ValueError(f"unknown JLPT level {jlpt!r}")
        return self._go(Page("browse", kind, "", row, common, jlpt=jlpt if kind == "grammar" else ""))

    def search(self, query: str, kind: str | None = "scope") -> View:
        """Search in the current scope (the kind being browsed), or in `kind` (None = everything)."""
        q = (query or "").strip()
        if not q:
            return self.view()
        if kind == "scope":
            kind = self.scope
        return self._go(Page("results", kind, q))

    def lookup(self, text: str) -> View:
        """Text selected elsewhere in the app. One match opens it directly; several give a list."""
        q = clean_selection(text)
        found = self.dic.lookup(q) if q else []
        page = Page("lookup", None, q, sel=found[0].id if len(found) == 1 else None)
        v = self._go(page)
        if len(found) == 1:
            self.dic.add_recent(found[0])
        return v

    def open_recent(self, entry) -> View:
        """An entry from RECENTS: a search for its headword with that entry selected."""
        self.dic.add_recent(entry)
        return self._go(Page("results", None, entry.headword, sel=entry.id))

    def goto(self, number: int) -> View:
        p = self.page
        if p.mode == "home":
            return self.view()
        self.history.replace(replace(p, number=max(0, number), sel=None))
        return self.view()

    def next_page(self) -> View:
        return self.goto(self.page.number + 1)

    def prev_page(self) -> View:
        return self.goto(self.page.number - 1)

    def select(self, entry_id: int):
        """The user clicked an entry. Kept in the history entry (so back / forward restore it) and added to recents."""
        e = self.dic.get(entry_id)
        if e is not None:
            self.history.replace(replace(self.page, sel=e.id))
            self.dic.add_recent(e)
        return e

    def back(self) -> View:
        self.history.back()
        return self.view()

    def forward(self) -> View:
        self.history.forward()
        return self.view()

    @property
    def can_back(self) -> bool:
        return self.history.can_back

    @property
    def can_forward(self) -> bool:
        return self.history.can_forward

    # -- computing what to show
    def _slice(self, found: list, p: Page):
        pages = max(1, math.ceil(len(found) / self.page_size))
        n = min(p.number, pages - 1)
        return found[n * self.page_size:(n + 1) * self.page_size], n, pages

    def view(self) -> View:
        p = self.page
        dic, size = self.dic, self.page_size
        if p.mode == "home":
            return View(p, "Recents", recents=dic.recents(), message="Nothing opened yet. Search above, or pick Vocab, Grammar or Kanji to browse.")
        if p.mode == "browse":
            total = dic.browse_count(p.kind, p.row, p.common, p.jlpt)
            pages = max(1, math.ceil(total / size))
            n = min(p.number, pages - 1)
            entries = dic.browse(p.kind, p.row, p.common, offset=n * size, limit=size, jlpt=p.jlpt)
            bits = [KIND_LABELS[p.kind]]
            if p.jlpt:
                bits.append(p.jlpt)
            if p.row:
                bits.append("other" if p.row == OTHER_ROW else f"{p.row} row")
            if p.common:
                bits.append("common only")
            title, empty = " \u00b7 ".join(bits), "No entries here."
        elif p.mode == "results":
            total = dic.search_count(p.query, p.kind)
            pages = max(1, math.ceil(total / size))
            n = min(p.number, pages - 1)
            entries = dic.search(p.query, p.kind, limit=size, offset=n * size)
            scope = f" in {KIND_LABELS[p.kind]}" if p.kind else ""
            title, empty = f"\u201c{p.query}\u201d{scope}", f"No match for \u201c{p.query}\u201d."
        elif p.mode == "lookup":
            found = dic.lookup(p.query) if p.query else []
            total = len(found)
            entries, n, pages = self._slice(found, p)
            title, empty = f"Lookup: \u201c{p.query}\u201d", f"No match for \u201c{p.query}\u201d."
        else:
            raise ValueError(f"unknown page mode {p.mode!r}")
        selected = next((e for e in entries if e.id == p.sel), None) or (entries[0] if entries else None)
        fixed = replace(p, number=n, sel=selected.id if selected else None)
        if fixed != p:
            self.history.replace(fixed)
        return View(fixed, title, entries, total, pages, selected, "" if entries else empty)


# --------------------------------------------------------------------------------------------------------------
# text for the detail panel and the status line (HTML for QLabel rich text)
# --------------------------------------------------------------------------------------------------------------

MUTED = "#85858d"
DOT = " \u00b7 "
COMMA = "\u3001"          # Japanese comma used between readings and written forms


def _e(s) -> str:
    return html.escape(str(s))


def pos_label(code: str) -> str:
    return POS_NAMES.get(code, code)


def entry_html(e: Entry) -> str:
    """All readings, all written forms and every sense of an entry. Nothing is collapsed to a single meaning."""
    out = [f"<div style='font-size:34px'><b>{_e(e.headword)}</b></div>"]
    tags = [KIND_LABELS.get(e.kind, e.kind)]
    if e.kind == "vocab" and e.common:
        tags.append("common")
    if e.jlpt:
        tags.append(f"JLPT {e.jlpt}")
    out.append(f"<div style='color:{MUTED}'>{_e(DOT.join(tags))}</div>")
    if e.kind == "vocab":
        readings = [r for r in e.readings if r]
        if readings:
            out.append(f"<div style='font-size:18px'>{_e(COMMA.join(readings))}</div>")
        others = [w for w in e.written_forms if w != e.headword]
        if others:
            out.append(f"<div style='color:{MUTED}'>Also written: {_e(COMMA.join(others))}</div>")
    elif e.reading and not (e.kind == "grammar" and e.reading == e.headword):
        out.append(f"<div style='font-size:18px'>{_e(e.reading)}</div>")
    if e.kind == "grammar" and e.extra.get("romaji"):
        out.append(f"<div style='color:{MUTED}'>{_e(e.extra['romaji'])}</div>")
    if e.kind == "kanji":
        bits = []
        if e.extra.get("onyomi"):
            bits.append(f"On: {_e(e.extra['onyomi'])}")
        if e.extra.get("kunyomi"):
            bits.append(f"Kun: {_e(e.extra['kunyomi'])}")
        if e.extra.get("strokes"):
            bits.append(f"{_e(e.extra['strokes'])} strokes")
        if bits:
            out.append(f"<div style='color:{MUTED}'>{'&nbsp;&nbsp;&nbsp;'.join(bits)}</div>")
    if e.kind == "grammar" and e.extra.get("connection"):
        out.append(f"<div style='color:{MUTED}'>Connection: {_e(e.extra['connection'])}</div>")
    senses = e.senses
    if senses:
        out.append("<div style='margin-top:8px'>")
        for i, s in enumerate(senses, 1):
            pos = ", ".join(pos_label(p) for p in s.get("pos", []))
            gloss = "; ".join(s.get("gloss", []))
            line = f"<b>{i}.</b> {_e(gloss)}" if len(senses) > 1 else _e(gloss)
            if pos:
                line += f" <span style='color:{MUTED}'>({_e(pos)})</span>"
            out.append(f"<div>{line}</div>")
        out.append("</div>")
    return "".join(out)


def sentences_html(result: SentenceResult) -> str:
    if not result.items:
        return f"<span style='color:{MUTED}'>No example sentences for this entry.</span>"
    parts = []
    for s in result.items:
        parts.append(f"<div style='margin-bottom:10px'><div style='font-size:16px'>{_e(s.ja)}</div>"
                     + (f"<div style='color:{MUTED}'>{_e(s.en)}</div>" if s.en else "") + "</div>")
    return "".join(parts)


def sentence_count_text(result: SentenceResult) -> str:
    """'3 of 37' (shown of available); 'of 400+' when the search stopped at its cap."""
    if result.total == 0:
        return "no examples"
    return f"{len(result.items)} of {result.total}{'+' if result.capped else ''}"


def row_texts(e: Entry) -> tuple:
    """(title line, detail line) for one list row. Entries with the same headword stay distinguishable by reading
    and meaning."""
    head = e.headword
    reading = COMMA.join(r for r in e.readings if r) if e.kind == "vocab" else e.reading
    if reading and reading != head:
        head = f"{head}  \u3010{reading}\u3011"
    tag = KIND_LABELS.get(e.kind, e.kind) if e.kind != "vocab" else ("common" if e.common else "")
    detail = e.short_meaning(110)
    return head, f"{detail}   \u00b7 {tag}" if tag else detail


def status_summary(st: DictStatus, dic: Dictionary) -> dict:
    """Everything the bottom bar and the banner say.
        name     'Japanese'
        counts   '219,000 words · 13,000 kanji · 10 grammar points · 250,000 example sentences'
        source   what it was built from, and when
        banner   a message to show prominently (empty when the dictionary is complete and current)
        command  the exact command to run, if there is something to do
        level    ok | warn | error"""
    counts = dic.counts()
    name = dic.meta.get("dictionary_name") or JAPANESE.name
    parts = [f"{counts.get('vocab', 0):,} words", f"{counts.get('kanji', 0):,} kanji",
             f"{counts.get('grammar', 0):,} grammar points", f"{counts.get('sentences', 0):,} example sentences"]
    sources = dic.meta.get("sources") or {}
    if st.using_builtin_sample:
        src = "Built-in sample only (not the full dictionary)"
    elif dic.is_sample:
        src = "Sample database (not the full dictionary)"
    else:
        labels = [v.get("label", k) for k, v in sources.items()]
        when = str(dic.meta.get("imported_at", "")).replace("T", " ").replace("Z", " UTC")
        src = ("Source: " + ", ".join(labels) if labels else "Imported dictionary") + (f"  \u00b7  imported {when}" if when else "")
        if st.state == "ready":
            src += "  \u00b7  up to date"
    level = {"ready": "ok"}.get(st.state, "error" if st.state in ("missing", "invalid") else "warn")
    return {"name": name, "counts": "  \u00b7  ".join(parts), "source": src, "banner": st.message, "command": st.command,
            "level": level, "state": st.state}


# --------------------------------------------------------------------------------------------------------------
# building the database from the downloaded files
# --------------------------------------------------------------------------------------------------------------

_COMMON_PRI = ("news1", "ichi1", "spec1", "spec2", "gai1")
_ENTITY_DEF = re.compile(r'<!ENTITY\s+([\w-]+)\s+"[^"]*">')
_ENTITY_REF = re.compile(r"&([\w-]+);")
_XML_BUILTIN = {"amp", "lt", "gt", "quot", "apos"}
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
MAX_SENTENCE_CHARS = 200
TAB = "\t"


class DictBuildError(Exception):
    """A source file is missing or malformed. The message says which file and what is wrong."""


def log(msg: str):
    print(msg, flush=True)


# --------------------------------------------------------------------------------------------------------------
# reading files
# --------------------------------------------------------------------------------------------------------------

def open_text(path):
    """Text stream for a plain, .gz or .bz2 file."""
    p = str(path)
    if p.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(p, "rb"), encoding="utf-8")
    if p.endswith(".bz2"):
        return io.TextIOWrapper(bz2.open(p, "rb"), encoding="utf-8")
    return open(p, encoding="utf-8", newline="")


_READ_ERRORS = (OSError, EOFError, zlib.error, UnicodeDecodeError)       # gzip.BadGzipFile is an OSError


def _damaged(what: str, path, exc: Exception) -> DictBuildError:
    return DictBuildError(f"{what} {path}: the file is damaged or incomplete ({exc.__class__.__name__}: {exc}). "
                          "Download it again.")


def _lines(path, what: str):
    """Yield lines, turning a damaged or truncated archive into a DictBuildError that names the file."""
    try:
        with open_text(path) as f:
            for line in f:
                yield line
    except _READ_ERRORS as exc:
        raise _damaged(what, path, exc) from exc


def file_facts(path) -> dict:
    p = Path(path)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return {"file": p.name, "size": p.stat().st_size, "sha256": h.hexdigest()}


def check_file(path, what: str) -> Path:
    p = Path(path)
    if not p.exists():
        raise DictBuildError(f"{what}: {p} does not exist.")
    if not p.is_file():
        raise DictBuildError(f"{what}: {p} is not a file.")
    if p.stat().st_size == 0:
        raise DictBuildError(f"{what}: {p} is empty.")
    return p


def _head_text(path, what: str, n: int = 65536) -> str:
    try:
        with open_text(path) as f:
            return f.read(n)
    except _READ_ERRORS as exc:
        raise DictBuildError(f"{what} {path}: cannot be read ({exc.__class__.__name__}: {exc}). Is it really a "
                             "gzip/bzip2/text file of the right kind? Download it again.") from exc


# --------------------------------------------------------------------------------------------------------------
# validation before anything is imported
# --------------------------------------------------------------------------------------------------------------

def _first_rows(path, what: str, limit: int = 60) -> list:
    rows = []
    for line in _lines(path, what):
        line = line.rstrip("\r\n")
        if line.strip():
            rows.append(line.split(TAB))
            if len(rows) >= limit:
                break
    return rows


def validate_source(key: str, path) -> Path:
    """Cheap checks that catch the usual mistakes before a long import: wrong or missing file, an HTML page saved
    instead of the data, a truncated download, the wrong kind of file. Raises DictBuildError."""
    spec = next(s for s in JAPANESE.sources if s.key == key)
    what = spec.label
    p = check_file(path, what)
    if key == "jmdict":
        if "<JMdict" not in _head_text(p, what):
            raise DictBuildError(f"{what}: {p} does not look like JMdict (no <JMdict> root element). "
                                 f"Expected {spec.filenames[0]} from {spec.url}")
    elif key == "kanjidic":
        if "<kanjidic2" not in _head_text(p, what):
            raise DictBuildError(f"{what}: {p} does not look like KANJIDIC2 (no <kanjidic2> root element). "
                                 f"Expected {spec.filenames[0]} from {spec.url}")
    elif key in ("tatoeba_jpn", "tatoeba_eng"):
        lang = "jpn" if key == "tatoeba_jpn" else "eng"
        rows = _first_rows(p, what)
        if not rows:
            raise DictBuildError(f"{what}: {p} has no data lines.")
        for r in rows:
            if len(r) < 3 or not r[0].isdigit():
                shown = TAB.join(r)[:80]
                raise DictBuildError(f"{what}: {p} must have three tab separated columns (id, language, text); found: {shown!r}")
            if r[1] != lang:
                raise DictBuildError(f"{what}: {p} holds language {r[1]!r}, expected {lang!r}. "
                                     "Did you swap the Japanese and English files?")
    elif key == "tatoeba_links":
        rows = _first_rows(p, what)
        if not rows:
            raise DictBuildError(f"{what}: {p} has no data lines.")
        for r in rows:
            if len(r) < 2 or not r[0].isdigit() or not r[1].isdigit():
                shown = TAB.join(r)[:80]
                raise DictBuildError(f"{what}: {p} must have two tab separated sentence ids per line; found: {shown!r}")
    return p


# --------------------------------------------------------------------------------------------------------------
# JMdict
# --------------------------------------------------------------------------------------------------------------

def _expand(text: str, names: set) -> bytes:
    if names:
        text = _ENTITY_REF.sub(lambda m: m.group(1) if m.group(1) in names else m.group(0), text)
    return text.encode("utf-8")


def _jmdict_xml(path):
    """Yield UTF-8 byte chunks of the JMdict XML with its DTD removed and part-of-speech entities replaced by their
    short codes ('v1', 'n', 'adj-i'). ElementTree cannot expand JMdict's ~100 thousand entity references (the XML
    parser limits entity expansion), and the codes are exactly what is stored."""
    names: set = set()
    header, in_header = [], True
    batch, size = [], 0
    for line in _lines(path, "JMdict"):
        if in_header:
            header.append(line)
            text = "".join(header)
            m = re.search(r"<JMdict[\s>]", text.replace("<!DOCTYPE JMdict", "<!DOCTYPE X"))
            if m:
                start = text.find("<!DOCTYPE")
                if start != -1:
                    end = text.find("]>", start)
                    names = set(_ENTITY_DEF.findall(text[start:end if end != -1 else None])) - _XML_BUILTIN
                    if end != -1:
                        text = text[:start] + text[end + 2:]
                yield text.encode("utf-8")
                in_header = False
            elif len(header) > 20000:
                raise DictBuildError(f"JMdict {path}: no <JMdict> root element found. Not a JMdict file?")
            continue
        batch.append(line)
        size += len(line)
        if size > 1 << 20:
            yield _expand("".join(batch), names)
            batch, size = [], 0
    if in_header:
        raise DictBuildError(f"JMdict {path}: no <JMdict> root element found. Not a JMdict file?")
    if batch:
        yield _expand("".join(batch), names)


def _pull_entries(chunks, tag: str, root_tag: str, what: str, path):
    """Yield each finished <tag> element from XML given as byte chunks. Checks the root element, turns XML errors
    into DictBuildError."""
    parser = ET.XMLPullParser(events=("start", "end"))
    root_checked = False
    try:
        for chunk in chunks:
            parser.feed(chunk)
            for ev, el in parser.read_events():
                if ev == "start" and not root_checked:
                    root_checked = True
                    if el.tag != root_tag:
                        raise DictBuildError(f"{what} {path}: root element is <{el.tag}>, expected <{root_tag}>.")
                elif ev == "end" and el.tag == tag:
                    yield el
        parser.close()
    except ET.ParseError as exc:
        raise DictBuildError(f"{what} {path}: malformed XML ({exc}).") from exc


def parse_jmdict(path, stats: dict | None = None):
    """Yield one dict per JMdict entry: headword, reading, kanji (written forms), kana (readings), senses
    [{pos, gloss, misc}], search_kanji (all written forms, including search-only ones), rank, freq."""
    stats = stats if stats is not None else {}
    stats.setdefault("skipped_entries", 0)
    n = 0
    for el in _pull_entries(_jmdict_xml(path), "entry", "JMdict", "JMdict", path):
        n += 1
        kebs = []                         # (text, search_only, priorities)
        for k in el.findall("k_ele"):
            t = (k.findtext("keb") or "").strip()
            if t:
                kebs.append((t, any((i.text or "") == "sK" for i in k.findall("ke_inf")), [p.text or "" for p in k.findall("ke_pri")]))
        rebs = []                         # (text, restrict-to-kanji list or None, no-kanji flag, priorities)
        for r in el.findall("r_ele"):
            t = (r.findtext("reb") or "").strip()
            if t:
                restr = [x.text for x in r.findall("re_restr") if x.text]
                rebs.append((t, restr or None, r.find("re_nokanji") is not None, [p.text or "" for p in r.findall("re_pri")]))
        senses, pos = [], []
        for s in el.findall("sense"):
            sp = [p.text for p in s.findall("pos") if p.text]
            if sp:
                pos = sp                  # JMdict lists the part of speech once; it applies to the senses that follow
            glosses = [g.text.strip() for g in s.findall("gloss")
                       if g.text and g.text.strip() and g.get(_XML_LANG, "eng") == "eng"]
            if glosses:
                senses.append({"pos": list(pos), "gloss": glosses, "misc": [m.text for m in s.findall("misc") if m.text]})
        if not rebs or not senses:
            stats["skipped_entries"] += 1
            el.clear()
            continue
        display = [t for t, search_only, _p in kebs if not search_only]
        headword = display[0] if display else (kebs[0][0] if kebs else rebs[0][0])
        reading = next((t for t, restr, nokanji, _p in rebs
                        if (restr is None or headword in restr) and not (nokanji and kebs)), rebs[0][0])
        pri = [p for _t, _s, ps in kebs for p in ps] + [p for *_x, ps in rebs for p in ps]
        nf = [int(m.group(1)) for p in pri for m in [re.fullmatch(r"nf(\d+)", p)] if m]
        yield {"headword": headword, "reading": reading, "kanji": display, "kana": [t for t, *_r in rebs],
               "search_kanji": [t for t, _s, _p in kebs], "senses": senses,
               "rank": 0 if any(p in _COMMON_PRI for p in pri) else 1, "freq": min(nf) if nf else 99}
        el.clear()
    if n == 0:
        raise DictBuildError(f"JMdict {path}: no <entry> elements found.")


# --------------------------------------------------------------------------------------------------------------
# KANJIDIC2
# --------------------------------------------------------------------------------------------------------------

def parse_kanjidic(path, stats: dict | None = None):
    stats = stats if stats is not None else {}
    stats.setdefault("skipped_kanji", 0)

    def chunks():
        buf = []
        for line in _lines(path, "KANJIDIC2"):
            buf.append(line)
            if len(buf) >= 4000:
                yield "".join(buf).encode("utf-8")
                buf = []
        if buf:
            yield "".join(buf).encode("utf-8")

    n = 0
    for el in _pull_entries(chunks(), "character", "kanjidic2", "KANJIDIC2", path):
        n += 1
        lit = (el.findtext("literal") or "").strip()
        on, kun, mean = [], [], []
        for r in el.iter("reading"):
            if r.text and r.get("r_type") == "ja_on":
                on.append(r.text)
            elif r.text and r.get("r_type") == "ja_kun":
                kun.append(r.text)
        for m in el.iter("meaning"):
            if m.text and m.get("m_lang") in (None, "en"):
                mean.append(m.text)
        if not lit:
            stats["skipped_kanji"] += 1
            el.clear()
            continue
        misc = el.find("misc")
        strokes = misc.findtext("stroke_count") if misc is not None else None
        grade = misc.findtext("grade") if misc is not None else None
        freq = misc.findtext("freq") if misc is not None else None
        yield {"headword": lit, "onyomi": "、".join(on), "kunyomi": "、".join(kun), "meaning": "; ".join(mean),
               "strokes": int(strokes) if strokes and strokes.isdigit() else None,
               "rank": 0 if grade and grade.isdigit() else 1, "freq": int(freq) if freq and freq.isdigit() else 99}
        el.clear()
    if n == 0:
        raise DictBuildError(f"KANJIDIC2 {path}: no <character> elements found.")


# --------------------------------------------------------------------------------------------------------------
# Tatoeba
# --------------------------------------------------------------------------------------------------------------

def _tsv(path, what: str, min_cols: int):
    for no, line in enumerate(_lines(path, what), 1):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(TAB)
        if len(parts) < min_cols:
            raise DictBuildError(f"{what} {path}, line {no}: expected {min_cols} tab separated columns, found {len(parts)}.")
        yield no, parts


def read_tatoeba(jpn_path, eng_path, links_path, stats: dict | None = None) -> list:
    """[(japanese, english)] with every translation linked to a Japanese sentence.

    * Links are treated as undirected pairs, so a file that lists (jpn, eng), (eng, jpn) or both works.
    * A Japanese sentence with several translations gives several pairs.
    * Duplicate links and duplicate (japanese, english) pairs are dropped.
    * A link to an English id that is not in the English file (a missing translation) is skipped and counted.
    * Sentences longer than MAX_SENTENCE_CHARS are skipped and counted."""
    stats = stats if stats is not None else {}
    for k in ("links", "duplicate_links", "missing_translations", "too_long", "duplicate_pairs"):
        stats.setdefault(k, 0)
    jpn = {}
    for no, p in _tsv(jpn_path, "Tatoeba Japanese sentences", 3):
        if not p[0].isdigit():
            raise DictBuildError(f"Tatoeba Japanese sentences {jpn_path}, line {no}: first column must be a number.")
        jpn[p[0]] = p[2]
    if not jpn:
        raise DictBuildError(f"Tatoeba Japanese sentences {jpn_path}: no sentences.")
    linked, seen = [], set()
    for no, p in _tsv(links_path, "Tatoeba links", 2):
        if not (p[0].isdigit() and p[1].isdigit()):
            raise DictBuildError(f"Tatoeba links {links_path}, line {no}: ids must be numbers.")
        stats["links"] += 1
        a, b = p[0], p[1]
        pair = (a, b) if a in jpn else (b, a) if b in jpn else None
        if pair is None:
            continue
        if pair in seen:
            stats["duplicate_links"] += 1
            continue
        seen.add(pair)
        linked.append(pair)
    want = {e for _j, e in linked}
    eng = {}
    for no, p in _tsv(eng_path, "Tatoeba English sentences", 3):
        if p[0] in want:
            eng[p[0]] = p[2]
    out, pairs = [], set()
    for jid, eid in linked:
        en = eng.get(eid)
        if not en:
            stats["missing_translations"] += 1
            continue
        ja = jpn[jid]
        if len(ja) > MAX_SENTENCE_CHARS or len(en) > MAX_SENTENCE_CHARS * 2:
            stats["too_long"] += 1
            continue
        if (ja, en) in pairs:
            stats["duplicate_pairs"] += 1
            continue
        pairs.add((ja, en))
        out.append((ja, en))
    return out


# --------------------------------------------------------------------------------------------------------------
# putting it together
# --------------------------------------------------------------------------------------------------------------

def _record(spec_key: str, path, entries: int) -> dict:
    spec = next(s for s in JAPANESE.sources if s.key == spec_key)
    d = file_facts(path)
    d.update(label=spec.label, url=spec.url, license=spec.license, entries=entries)
    return d


def build(out_path, *, jmdict=None, kanjidic=None, tatoeba=None, grammar=None, sample: bool = False,
          progress=lambda msg: None) -> dict:
    """Build the dictionary into a temporary file and replace out_path only when everything succeeded.

    tatoeba = (jpn_sentences, eng_sentences, links). Returns the counts and the skipped-record statistics.
    Raises DictBuildError (or OSError) and leaves out_path as it was."""
    out_path = Path(out_path)
    if not sample and not (jmdict or kanjidic or tatoeba):
        raise DictBuildError("Nothing to import: give at least one of JMdict, KANJIDIC2 or the Tatoeba files.")
    # validate every input first: a wrong file should not cost a long import
    paths = {}
    if jmdict:
        paths["jmdict"] = validate_source("jmdict", jmdict)
    if kanjidic:
        paths["kanjidic"] = validate_source("kanjidic", kanjidic)
    if tatoeba:
        if len(tatoeba) != 3 or not all(tatoeba):
            raise DictBuildError("Tatoeba needs all three files: Japanese sentences, English sentences and the links.")
        for key, p in zip(("tatoeba_jpn", "tatoeba_eng", "tatoeba_links"), tatoeba):
            paths[key] = validate_source(key, p)
    grammar = grammar or (GRAMMAR_FILE if GRAMMAR_FILE.exists() else None)       # the bundled JLPT list unless told otherwise
    grammar_recs = load_grammar_file(grammar) if grammar else []
    replaces_sample = bool(grammar) and Path(grammar).suffix.lower() != ".json"    # a CSV is the whole list; a JSON file adds to the sample

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".building")
    tmp.unlink(missing_ok=True)
    stats: dict = {"vocab": 0, "kanji": 0, "grammar": 0, "sentences": 0}
    sources: dict = {}
    con = sqlite3.connect(tmp)
    try:
        con.execute("PRAGMA journal_mode = OFF")
        con.execute("PRAGMA synchronous = OFF")
        create_schema(con)
        seed = load_seed()
        seed["grammar"] = ([] if replaces_sample else list(seed.get("grammar", []))) + grammar_recs
        if sample:
            insert_seed(con, seed)
            stats.update(vocab=len(seed["vocab"]), kanji=len(seed["kanji"]), grammar=len(seed["grammar"]),
                         sentences=len(seed["sentences"]))
        else:
            if jmdict:
                progress("Reading JMdict ...")
                for e in parse_jmdict(paths["jmdict"], stats):
                    gloss = " | ".join("; ".join(s["gloss"]) for s in e["senses"])
                    forms = [(t, 0) for t in e["search_kanji"]] + [(t, 1) for t in e["kana"]]
                    if not e["search_kanji"]:
                        forms += [(t, 0) for t in e["kana"]]
                    add_entry(con, "vocab", e["headword"], e["reading"], gloss,
                              {"kanji": e["kanji"], "kana": e["kana"], "senses": e["senses"]},
                              rank=e["rank"], freq=e["freq"], forms=forms)
                    stats["vocab"] += 1
                    if stats["vocab"] % 50000 == 0:
                        progress(f"  {stats['vocab']:,} entries ...")
                if stats["vocab"] == 0:
                    raise DictBuildError(f"JMdict {paths['jmdict']}: no usable entries (each needs a reading and an English meaning).")
                sources["jmdict"] = _record("jmdict", paths["jmdict"], stats["vocab"])
            if kanjidic:
                progress("Reading KANJIDIC2 ...")
                for k in parse_kanjidic(paths["kanjidic"], stats):
                    reading = "、".join(x for x in (k["onyomi"], k["kunyomi"]) if x)
                    rs = [r for r in reading.split("、") if r]
                    forms = [(k["headword"], 0)] + [(r, 1) for r in rs] + [(r.split(".")[0], 1) for r in rs if "." in r] \
                        + [(r.replace(".", "").replace("-", ""), 1) for r in rs]
                    add_entry(con, "kanji", k["headword"], reading, k["meaning"],
                              {"onyomi": k["onyomi"], "kunyomi": k["kunyomi"], "strokes": k["strokes"]},
                              rank=k["rank"], freq=k["freq"], forms=forms)
                    stats["kanji"] += 1
                sources["kanjidic"] = _record("kanjidic", paths["kanjidic"], stats["kanji"])
            insert_seed(con, {"grammar": seed["grammar"]}, kinds=("grammar",), with_sentences=False)
            stats["grammar"] = len(seed["grammar"])
            if tatoeba:
                progress("Reading Tatoeba ...")
                pairs = read_tatoeba(paths["tatoeba_jpn"], paths["tatoeba_eng"], paths["tatoeba_links"], stats)
                if not pairs:
                    raise DictBuildError("Tatoeba: no Japanese sentence has an English translation in these files. "
                                         "Check that the links file belongs to these sentence files.")
                con.executemany("INSERT OR IGNORE INTO sentences (ja, en) VALUES (?, ?)", pairs)
                stats["sentences"] = len(pairs)
                for key in ("tatoeba_jpn", "tatoeba_eng", "tatoeba_links"):
                    sources[key] = _record(key, paths[key], len(pairs))
        progress("Creating indexes ...")
        fts = finish_database(con)
        if not fts:
            progress("  (this SQLite has no FTS5 trigram index: example sentences use a slower scan)")
        counts = compute_counts(con)
        set_meta(con, "schema_version", str(SCHEMA_VERSION))
        set_meta(con, "dictionary_id", JAPANESE.id)
        set_meta(con, "dictionary_name", JAPANESE.name)
        set_meta(con, "source", "sample" if sample else "imported")
        set_meta(con, "imported_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        set_meta(con, "sources", sources)
        set_meta(con, "counts", counts)
        set_meta(con, "skipped", {k: v for k, v in stats.items() if k not in ("vocab", "kanji", "grammar", "sentences", "links")})
        con.commit()
        bad = con.execute("PRAGMA quick_check").fetchone()[0]
        if bad != "ok":
            raise DictBuildError(f"The new database failed its integrity check ({bad}); the old one was kept.")
        con.execute("VACUUM")
    except BaseException:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    tmp.replace(out_path)                 # atomic on the same filesystem
    stats["path"] = str(out_path)
    return stats


# --------------------------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------------------------

def detection_report(found: dict, directory: Path) -> list:
    lines = [f"Source folder: {directory}"]
    for s in JAPANESE.sources:
        p = found.get(s.key)
        lines.append(f"  [{'found  ' if p else 'MISSING'}] {s.label:<34} " + (p.name if p else f"expected {s.filenames[0]}  ({s.url})"))
    return lines


def report_check(out: Path, directory: Path | None) -> int:
    st = inspect_db(out, JAPANESE, directory)
    log(f"Dictionary file: {out}")
    log(f"State: {st.state}" + ("  (valid, complete, up to date)" if st.state == "ready" else ""))
    info = st.info
    if info:
        c = info.get("counts", {})
        log(f"Schema version: {info.get('schema_version')}   sentence index: {'yes' if info.get('fts') else 'no (slower sentence lookup)'}")
        log(f"Counts: {c.get('vocab', 0):,} words, {c.get('kanji', 0):,} kanji, {c.get('grammar', 0):,} grammar points, "
            f"{c.get('sentences', 0):,} sentences")
        log(f"Imported: {info.get('imported_at') or 'unknown'}")
        for key, s in (info.get("sources") or {}).items():
            log(f"  source {key}: {s.get('label', '')} - {s.get('file', '')} ({s.get('size', 0):,} bytes, "
                f"sha256 {str(s.get('sha256', ''))[:12]}..., {s.get('entries', 0):,} records, {s.get('license', '')})")
        if not info.get("sources"):
            log("  no source files recorded")
    if st.message:
        log(st.message)
    return {"ready": 0, "missing": 1, "invalid": 1}.get(st.state, 2)


# --------------------------------------------------------------------------------------------------------------
# downloading the source files:  python ocr_dict.py download
# --------------------------------------------------------------------------------------------------------------

# Where YOUR copies of the five files live (a GitHub release named "data"). Edit this line after you have uploaded them,
# or set $OCR_SCANNER_DATA_URL, or pass --mirror. While it still says YOUR-USER only the original sources are used.
DATA_MIRROR = "https://github.com/rudraaaaa-05/text_ocr_scanner/releases/download/data/"


def mirror_url(override: str | None = None) -> str:
    url = (override or os.environ.get("OCR_SCANNER_DATA_URL", "") or DATA_MIRROR).strip()
    return "" if not url or "YOUR-USER" in url else url.rstrip("/") + "/"


def download_file(url: str, dest, progress=lambda msg: None, timeout: float = 60) -> Path:
    """Fetch url into dest. Written as dest.part and renamed when complete, so a failed download leaves nothing behind."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "ocr-scanner-dictionary-downloader"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done, next_report = 0, 10 << 20
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done >= next_report:
                    progress(f"    {done >> 20} MB" + (f" of {total >> 20} MB" if total else ""))
                    next_report += 10 << 20
        if total and done != total:
            raise DictBuildError(f"{url}: the download stopped after {done:,} of {total:,} bytes.")
        os.replace(tmp, dest)
    except (OSError, ValueError) as exc:                # URLError and HTTPError are OSErrors
        tmp.unlink(missing_ok=True)
        raise DictBuildError(f"{url}: {exc}") from exc
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return dest


def download_sources(directory, *, mirror: str = "", upstream_only: bool = False, force: bool = False, only=None,
                     progress=lambda msg: None) -> dict:
    """Fetch the source files into `directory` under the names the importer looks for. For each file the mirror is
    tried first (unless upstream_only), then the original source. A file is kept only when validate_source accepts it.
    only = source keys or groups to fetch (default: all). Returns {source key: "downloaded" | "present" | error text}."""
    directory = Path(directory)
    wanted = {o.lower() for o in only} if only else None
    present = find_sources(JAPANESE, directory)
    mirror = "" if upstream_only else mirror_url(mirror)
    results: dict = {}
    for spec in JAPANESE.sources:
        if wanted and spec.key.lower() not in wanted and spec.group.lower() not in wanted:
            continue
        if present[spec.key] and not force:
            results[spec.key] = "present"
            progress(f"{spec.label}: already in {directory} ({present[spec.key].name}), skipped (use --force to fetch again)")
            continue
        name = spec.url.rsplit("/", 1)[-1]                          # JMdict_e.gz, jpn_sentences.tsv.bz2 ...
        urls = ([mirror + name] if mirror else []) + [spec.url]
        errors = []
        for url in urls:
            progress(f"{spec.label}: {url}")
            dest = directory / name
            try:
                download_file(url, dest, progress)
                validate_source(spec.key, dest)
            except DictBuildError as exc:
                dest.unlink(missing_ok=True)
                errors.append(str(exc))
                if url != urls[-1]:
                    progress("    failed, trying the next source ...")
                continue
            results[spec.key] = "downloaded"
            progress(f"    saved {dest} ({dest.stat().st_size:,} bytes)")
            break
        else:
            results[spec.key] = " | ".join(errors)
    return results


def download_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="ocr_dict.py download", description="Download the dictionary source files "
                                 "(JMdict, KANJIDIC2, Tatoeba) into the source folder.")
    ap.add_argument("--dir", help="folder to save into (default: data/dictionary or $OCR_SCANNER_DICT_DIR)")
    ap.add_argument("--mirror", help="base URL of your own copies (default: DATA_MIRROR in this file or $OCR_SCANNER_DATA_URL)")
    ap.add_argument("--upstream", action="store_true", help="ignore the mirror and use the original sources only "
                    "(what you run to collect the files you then upload to your own repository)")
    ap.add_argument("--force", action="store_true", help="download again even when the file is already there")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="only these sources or groups: jmdict kanjidic tatoeba "
                    "(or tatoeba_jpn, tatoeba_eng, tatoeba_links)")
    a = ap.parse_args(argv)
    directory = Path(a.dir).expanduser() if a.dir else source_dir()
    if a.only:
        known = {x for s in JAPANESE.sources for x in (s.key, s.group)}
        unknown = [o for o in a.only if o.lower() not in known]
        if unknown:
            log(f"Unknown source name: {', '.join(unknown)}. Choose from: {', '.join(sorted(known))}")
            return 2
    where = "the original sources" if a.upstream or not mirror_url(a.mirror) else f"{mirror_url(a.mirror)} (then the original sources)"
    log(f"Saving into {directory}\nUsing {where}\n")
    results = download_sources(directory, mirror=a.mirror or "", upstream_only=a.upstream, force=a.force, only=a.only, progress=log)
    failed = {k: v for k, v in results.items() if v not in ("downloaded", "present")}
    if failed:
        log("\nNot downloaded:")
        for k, v in failed.items():
            log(f"  {k}: {v}")
        return 1
    log("\nDone. Now build the dictionary with:  python ocr_dict.py")
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "download":
        return download_main(argv[1:])
    if argv and argv[0] == "build":                    # "python ocr_dict.py build" is the same as "python ocr_dict.py"
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="ocr_dict.py", description="Build the Japanese dictionary (JMdict, KANJIDIC2, Tatoeba).",
                                 epilog="With no source options, the files in the source folder are detected automatically. Get the files first with:  python ocr_dict.py download")
    ap.add_argument("--out", help="dictionary file to write (default: the app's dictionary file)")
    ap.add_argument("--source-dir", help="folder with the downloaded files (default: data/dictionary or $OCR_SCANNER_DICT_DIR)")
    ap.add_argument("--jmdict", help="JMdict_e (.gz, .bz2 or plain XML)")
    ap.add_argument("--kanjidic", help="kanjidic2.xml (.gz, .bz2 or plain)")
    ap.add_argument("--tatoeba-jpn", help="jpn_sentences.tsv")
    ap.add_argument("--tatoeba-eng", help="eng_sentences.tsv")
    ap.add_argument("--tatoeba-links", help="jpn-eng_links.tsv (two columns of sentence ids)")
    ap.add_argument("--grammar", help="grammar list: a JLPT CSV (replaces data/grammar_jlpt.csv) or a JSON file of extra points")
    ap.add_argument("--sample", action="store_true", help="build only the small bundled sample")
    ap.add_argument("--check", action="store_true", help="report whether the installed dictionary is valid, complete and current")
    for old in ("--jlpt-vocab", "--jlpt-kanji"):
        ap.add_argument(old, help=argparse.SUPPRESS)           # JLPT lists are no longer used; accepted and ignored
    a = ap.parse_args(argv)

    for old, val in (("--jlpt-vocab", a.jlpt_vocab), ("--jlpt-kanji", a.jlpt_kanji)):
        if val:
            log(f"Note: {old} is ignored. JLPT levels are not used any more.")
    directory = Path(a.source_dir).expanduser() if a.source_dir else source_dir()
    out = Path(a.out).expanduser() if a.out else default_paths()[0]
    if a.check:
        return report_check(out, directory)

    tat = (a.tatoeba_jpn, a.tatoeba_eng, a.tatoeba_links)
    explicit = any((a.jmdict, a.kanjidic, *tat))
    jm, kd = a.jmdict, a.kanjidic
    if not explicit and not a.sample:
        found = find_sources(JAPANESE, directory)
        for line in detection_report(found, directory):
            log(line)
        jm, kd = found["jmdict"], found["kanjidic"]
        tat = (found["tatoeba_jpn"], found["tatoeba_eng"], found["tatoeba_links"])
        if not any((jm, kd, *tat)):
            log("\nNo source files found. Download them with:  python ocr_dict.py download   "
                "or pass the files explicitly (see --help).")
            return 1
    if any(tat) and not all(tat):
        have = [n for n, p in zip(("jpn_sentences", "eng_sentences", "jpn-eng_links"), tat) if p]
        log(f"Tatoeba needs all three files (Japanese sentences, English sentences, links); only got: {', '.join(have)}.")
        return 1
    try:
        stats = build(out, jmdict=jm, kanjidic=kd, tatoeba=tat if all(tat) else None, grammar=a.grammar, sample=a.sample,
                      progress=log)
    except (DictBuildError, OSError) as exc:
        log(f"\nERROR: {exc}\nThe existing dictionary was not changed.")
        return 1
    log(f"\nDatabase: {stats['path']}")
    log(f"Imported: {stats['vocab']:,} words, {stats['kanji']:,} kanji, {stats['grammar']:,} grammar points, "
        f"{stats['sentences']:,} example sentences")
    skipped = {k: v for k, v in stats.items() if k not in ("vocab", "kanji", "grammar", "sentences", "path", "links") and v}
    if skipped:
        log("Skipped: " + ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in skipped.items()))
    if not a.sample and not (jm and kd and all(tat)):
        log("Note: the dictionary is incomplete (some sources were not imported). Run --check for details.")
    log("Restart the app, or press Reload in the Dictionary tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
