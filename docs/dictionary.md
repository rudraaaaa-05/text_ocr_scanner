# Japanese Dictionary

The Dictionary tab provides offline search and browsing for Japanese vocabulary, grammar, and kanji. Entries include readings and meanings; vocabulary can also include example sentences. Browse in kana order, filter vocabulary by kana row or common-word status, and filter grammar by JLPT level. **Recents** and Back/Forward navigation help revisit entries. A search started within a category stays scoped to that category.

## Search and lookup

- Search using Japanese text, romaji, or English meanings.
- If your system cannot enter Japanese into the search field, toggle the **あ** button and type romaji to enter kana (for example, `taberu` becomes `たべる`). Press Enter to finish a trailing `n`.
- Grammar can also be searched by romaji, such as `tai` or `sou da`.
- Select text in Capture output or the Table Cleaner preview and choose **Lookup** to open matching dictionary results.

The status bar shows the dictionary state, entry counts, sources, and build time. If the full dictionary is unavailable, the tab explains how to build it and can display a small built-in sample.

## Install or refresh the dictionary

Run these commands from the repository root, using the same Python environment as the app:

```bash
python ocr_dict.py download
python ocr_dict.py
```

The first command downloads the five source files into `data/dictionary/` by default, or into the folder set by `OCR_SCANNER_DICT_DIR`. It tries this repository's configured release mirror first, then falls back to the original sources. Existing valid files are skipped; use `--force` to fetch them again.

Useful download options:

```bash
python ocr_dict.py download --only jmdict kanjidic
python ocr_dict.py download --dir /path/to/source-files
python ocr_dict.py download --upstream
```

`--only` accepts source names or groups (`jmdict`, `kanjidic`, `tatoeba`); `--upstream` bypasses the mirror. JMdict and KANJIDIC2 are compressed XML files, and Tatoeba files are compressed TSVs; the importer reads them without manual extraction.

The second command validates the downloaded files and builds the database. It writes the replacement database only after the new one passes integrity checks; a failed build leaves the current database in place. Notes and recent entries are stored separately and are not overwritten by a rebuild.

Check the installed database with:

```bash
python ocr_dict.py --check
```

Exit status `0` means the database is complete and current, `2` means usable but incomplete/out of date/sample, and `1` means missing or invalid. The app also checks the database on startup and when **Reload** is pressed.

## Sources and licences

| Data | Source | File(s) | Licence |
|---|---|---|---|
| Vocabulary and English meanings | [JMdict](https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project), EDRDG | `JMdict_e.gz` | [CC BY-SA 4.0](https://www.edrdg.org/edrdg/licence.html) |
| Kanji | [KANJIDIC2](https://www.edrdg.org/wiki/index.php/KANJIDIC_Project), EDRDG | `kanjidic2.xml.gz` | CC BY-SA 4.0 |
| Example sentences and translations | [Tatoeba](https://tatoeba.org/downloads) contributors | `jpn_sentences.tsv.bz2`, `eng_sentences.tsv.bz2`, `jpn-eng_links.tsv.bz2` | [CC BY 2.0 FR](https://tatoeba.org/terms_of_use); some sentences are CC0 |
| JLPT grammar points | Bundled CSV | `data/grammar_jlpt.csv` | Verify the licence of your copy before redistributing it |

Keep the data attribution and share-alike terms when redistributing the sources or a database built from them. See [NOTICE.md](../NOTICE.md). The grammar CSV is separate from the sources downloaded above; do not redistribute it unless its own terms allow that.

## Hosting a release mirror

To host source copies on GitHub, publish the five downloaded files as assets on a release with the tag `data`. The repository includes an automatic monthly workflow at `.github/workflows/update-data.yml`; run it manually once after pushing the workflow to the default branch. The workflow refreshes the release assets monthly.

Alternatively, create the release with GitHub CLI:

```bash
python ocr_dict.py download --upstream --dir release_files
gh release create data --title "Dictionary data" \
  --notes "Source files; see NOTICE.md for attribution." release_files/*
```

Configure the app's `DATA_MIRROR` in `ocr_dict.py` to point to:

```text
https://github.com/<owner>/<repo>/releases/download/data/
```

You can override the configured mirror per invocation with `--mirror URL` or set `OCR_SCANNER_DATA_URL`. Downloads fall back to upstream if the mirror is unavailable.

## Grammar data

The bundled JLPT grammar CSV contains grammar expressions, romaji, and meanings. If it is absent, the app uses its built-in grammar sample. If you replace it or redistribute it, make sure your copy's licence permits that use.
