# Table Cleaner

The Table Cleaner turns the OCR output log into a Markdown table. The log may contain repeated captures, noise, timestamps, and sentences split across wrapped lines. Cleaning creates a separate file; it never changes the original.

## Use it

1. Open the **Table Cleaner** tab.
2. Select the OCR output file to clean.
3. Choose how to organize the rows:
   - **Line by line** keeps one row per OCR line.
   - **Sentence by sentence** (default) joins likely wrapped lines and makes one row per sentence.
   - **Word by word (vocabulary list)** makes one row per vocabulary word and includes its reading and source sentence.
4. Choose the file language, and adjust options such as **Ignore furigana** or **Translate to English** if needed.
5. Choose **Clean**. The resulting `.md` file is saved next to the source file. For example:

   ```text
   ocr_output_2026-01-31_14-05-09.txt
   -> ocr_output_2026-01-31_14-05-09_table.md
   ```

The original is never overwritten. If a table with that name already exists, the cleaner uses `_table_2.md`, `_table_3.md`, and so on.

## Output modes

### Sentence by sentence

The cleaner attempts to rejoin lines split by screen wrapping, then separates lines containing multiple sentences. With translation enabled, output includes an English column; timestamps are included in a Time column when present.

| # | Sentence | English |
|--:|----------|---------|
| 1 | こないだカフェーにいった。 | I went to a cafe the other day. |
| 2 | 覚えていない。 | I don't remember. |

### Word by word

Each vocabulary word appears once, in dictionary form, with its reading and the sentence it came from. Particles, endings, numbers, and people's names are excluded. When translation is enabled, words are translated as used in their sentence.

| # | Word | Reading | English | Sentence |
|--:|------|---------|---------|----------|
| 1 | 行く | いく | to go | こないだカフェーにいった。 |

Word mode works with languages that separate words with spaces. Japanese requires SudachiPy:

```bash
pip install -r requirements-vocab.txt
```

The tab displays the install command for the active Python environment if the package is missing. Chinese and Thai word segmentation are not supported; use sentence or line mode.

## Options and cleanup

- **Language of the file**: choose the language used in the OCR text. Auto-detect is the default.
- **Ignore furigana**: in sentence/word modes, short hiragana reading lines above kanji are removed. Removed lines are listed in the output so you can check them; turn this option off if it removes real text. SudachiPy makes the check more reliable for Japanese.
- **Translate to English**: adds English translations. Translation requires a provider that supports it; Local OCR cannot translate. Choose **Automatic** to use the active provider and its fallbacks, or select a provider. If using a cloud service, the OCR text is sent to that provider, not screenshots. Ollama can translate locally.
- Blank lines and lines containing only symbols are dropped. Duplicate text is removed while preserving its first occurrence. Whitespace between Japanese/Chinese characters is removed, and Markdown-sensitive characters are escaped.
- Timestamps are moved to a Time column when present.

## Limits and troubleshooting

Line wrapping is inferred from text because OCR output does not retain line positions. The cleaner avoids joining headings, list items, and short lines, but unusual layouts or multiple columns may still be combined incorrectly or left split. If the output looks wrong, try line-by-line mode or adjust the source selection.

If translation misses some rows, they are marked `_(not translated)_`; rerun the cleaner or choose another provider. A run is limited to 2,000 translation rows. **Cancel** stops a translation and saves no output. English input does not need translation and does not get an English column.

For Japanese word mode, install SudachiPy using the command above. If translation is unavailable, configure a translation-capable provider in **OCR Mode**, or turn translation off.
