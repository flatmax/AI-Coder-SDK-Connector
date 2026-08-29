import { describe, it, expect } from 'vitest';
import { readPreference, writePreference } from './settings-preferences.js';

// The two files these functions are actually pointed at, verbatim from
// `src/aic_dc/config/`. Written out rather than reduced to the keys under
// test, because what `writePreference` has to protect is everything it is
// *not* asked to change — and a fixture with only the target key in it
// cannot fail that way.
const ENGINE_JSON = `{
  "model": null,
  "commit_model": null,
  "permission_mode": null,
  "effort": null,
  "thinking_display": null,
  "max_budget_usd": null,
  "cli_path": null,
  "max_buffer_size": null
}
`;

const APP_JSON = `{
  "doc_convert": {
    "enabled": true,
    "extensions": [".docx", ".pdf", ".pptx", ".xlsx", ".csv", ".rtf", ".odt", ".odp"],
    "max_source_size_mb": 50
  },
  "doc_index": {
    "keyword_model": "BAAI/bge-small-en-v1.5",
    "keywords_enabled": true,
    "keywords_top_n": 3,
    "keywords_ngram_range": [1, 2],
    "keywords_min_section_chars": 50,
    "keywords_min_score": 0.3,
    "keywords_diversity": 0.5,
    "keywords_tfidf_fallback_chars": 150,
    "keywords_max_doc_freq": 0.6
  },
  "history": {
    "session_dir_warning_bytes": 1073741824,
    "mirror_gap_tolerance": 3
  }
}
`;

describe('readPreference', () => {
  it('reads a top-level key', () => {
    const content = ENGINE_JSON.replace('"thinking_display": null', '"thinking_display": "omitted"');
    expect(readPreference(content, ['thinking_display'], '')).toBe('omitted');
  });

  it('reads a nested key', () => {
    expect(readPreference(APP_JSON, ['doc_index', 'keywords_enabled'], true)).toBe(true);
  });

  it('falls back for an explicit null, an absent key and an absent section', () => {
    expect(readPreference(ENGINE_JSON, ['thinking_display'], '')).toBe('');
    expect(readPreference(ENGINE_JSON, ['no_such_key'], 'x')).toBe('x');
    expect(readPreference('{}', ['doc_index', 'keywords_enabled'], true)).toBe(true);
  });

  it('falls back rather than throwing on content that will not parse', () => {
    expect(readPreference('{ "a":', ['a'], 'fallback')).toBe('fallback');
    expect(readPreference('[1, 2]', ['a'], 'fallback')).toBe('fallback');
    expect(readPreference('', ['a'], 'fallback')).toBe('fallback');
  });
});

describe('writePreference', () => {
  it('changes one line and leaves the rest of the file byte-identical', () => {
    const next = writePreference(ENGINE_JSON, ['thinking_display'], 'omitted');
    expect(next).toContain('"thinking_display": "omitted"');
    // The check that matters: every *other* line survived unchanged.
    const before = ENGINE_JSON.split('\n');
    const after = next.split('\n');
    expect(after.length).toBe(before.length);
    for (let i = 0; i < before.length; i += 1) {
      if (before[i].includes('thinking_display')) continue;
      expect(after[i]).toBe(before[i]);
    }
  });

  it('does not reformat the arrays a stringify round trip would explode', () => {
    // The whole reason writes edit text rather than re-serialising: this
    // one line becomes twelve under `JSON.stringify(_, null, 2)`, and a
    // click on a toggle is not a licence to rewrite a user's file.
    const next = writePreference(APP_JSON, ['doc_index', 'keywords_enabled'], false);
    expect(next).toContain(
      '"extensions": [".docx", ".pdf", ".pptx", ".xlsx", ".csv", ".rtf", ".odt", ".odp"],',
    );
    expect(next).toContain('"keywords_ngram_range": [1, 2],');
    expect(next).toContain('"keywords_enabled": false,');
  });

  it('keeps the trailing comma, or its absence', () => {
    expect(writePreference(ENGINE_JSON, ['model'], 'opus')).toContain('"model": "opus",');
    expect(writePreference(ENGINE_JSON, ['max_buffer_size'], 4)).toContain(
      '"max_buffer_size": 4\n',
    );
  });

  it('round-trips through JSON.parse to the value asked for', () => {
    const next = writePreference(APP_JSON, ['doc_index', 'keywords_enabled'], false);
    expect(JSON.parse(next).doc_index.keywords_enabled).toBe(false);
    expect(JSON.parse(next).doc_convert.extensions).toHaveLength(8);
  });

  it('writes null as null, which is how "let the CLI decide" is spelled', () => {
    const set = writePreference(ENGINE_JSON, ['thinking_display'], 'summarized');
    const cleared = writePreference(set, ['thinking_display'], null);
    expect(JSON.parse(cleared).thinking_display).toBeNull();
    expect(cleared).toBe(ENGINE_JSON);
  });

  it('refuses content that is not a JSON object', () => {
    // Null is the caller's signal to send the reader to the textarea,
    // which is the surface that can fix a file that does not parse. A
    // switch that "fixed" it would replace the file with its own idea
    // of what it should contain.
    expect(writePreference('{ "a":', ['a'], 1)).toBeNull();
    expect(writePreference('[1, 2]', ['a'], 1)).toBeNull();
    expect(writePreference('"just a string"', ['a'], 1)).toBeNull();
  });

  it('creates a key, and the section holding it, when the file lacks both', () => {
    const next = writePreference('{}', ['doc_index', 'keywords_enabled'], false);
    expect(JSON.parse(next).doc_index.keywords_enabled).toBe(false);
  });

  it('falls back to a reserialise when the leaf name is ambiguous', () => {
    // Two `"enabled":` lines under different sections: the line search
    // and `JSON.parse` would disagree about which one is the value, and
    // the one that would be wrong is the write.
    const content = '{\n  "a": { "enabled": true },\n  "b": { "enabled": true }\n}';
    const next = writePreference(content, ['b', 'enabled'], false);
    expect(JSON.parse(next).a.enabled).toBe(true);
    expect(JSON.parse(next).b.enabled).toBe(false);
  });

  it('falls back rather than editing a line whose value is a container', () => {
    const content = '{\n  "ngram": [1, 2]\n}';
    const next = writePreference(content, ['ngram'], [1, 3]);
    expect(JSON.parse(next).ngram).toEqual([1, 3]);
  });
});
