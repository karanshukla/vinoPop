# Comment style

A comment is the last resort. Work down this ladder, and write prose only
when all four rungs fail.

1. **Extract it.** Logic that a comment explains belongs in a function with
   a domain name. Arithmetic spelled out in a comment belongs in named
   constants. A coercion that is explained twice belongs in one named helper.

   ```python
   # Before
   if delay > 4000:  # 4 s: long enough to move the pointer to the bar
   return ctypes.CDLL(None).prctl(15, name, 0, 0, 0)  # PR_SET_NAME

   # After
   if delay > INITIAL_HIDE_MS:
   return ctypes.CDLL(None).prctl(PR_SET_NAME, name, 0, 0, 0)
   ```

2. **Name it.** A comment that labels a block (`# Phase 2: cache lookup`)
   means the block wants to be a function. Sentinels get names too: write
   `USE_THEME_DEFAULT = ""`, not `""  # empty means the theme's colour`.

3. **Pin it with a test pair.** A comment that states a business rule is a
   rule nothing enforces. Write one test inside the boundary and one
   outside, named after the rule (`test_a_two_character_selection_shows_the_popup`
   / `test_a_one_character_selection_does_not`), instead of writing
   `# one character is usually a misclick`. The pair is the point: it fails
   the day someone moves the limit. In a test, a comment that labels the
   next few asserts is the same smell. Split those asserts into their own
   test, named after what the comment said.

4. **Pin cross-boundary rules with an end-to-end test.** What `install.sh`
   writes, the `vinopop` command line, the D-Bus API and the settings file
   round-trip belong in tests like `tests/test_install_sh.py` and
   `tests/test_remote.py`, not in a paragraph.

5. **Only then, prose.** Hidden constraints, upstream bugs, protocol
   requirements, incident history that still constrains the code. State the
   constraint, not its biography: "GTK draws SVG icons only with
   librsvg2-common installed" is enough without the story of how we found
   out. Measured claims carry a date.

Docstrings follow the same rule: say what a function is for and anything
the signature can't, not what the signature already says or how it got
that way.

## Linking the test from the code

When rung 3 or 4 replaced a comment, the code it governs says which test
pins the rule, always with the same phrase:

```python
def should_show(text: str) -> bool:
    """Whether a selection is long enough to pop up for (pinned by
    `tests/test_min_selection.py::test_two_characters_show_the_popup`,
    `tests/test_min_selection.py::test_one_character_does_not`)."""
```

`tests/test_doc_links.py` (CI job "Doc Links") checks every
`` `tests/test_x.py::test_name` `` (or `` `tests/test_x.py::TestClass::test_name` ``)
in a comment or docstring: the file must exist and define that test.

## Exempt

- Docstrings that say what a module, class or function is and how to call
  it. They follow the same "constraint, not biography" rule, but aren't
  replaced by code.
- `# noqa`, `# type: ignore` and `# pragma: no cover`, each with its reason,
  and `# nosec` for Bandit.
