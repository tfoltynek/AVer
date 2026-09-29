"""Every user-facing string has a Czech translation.

The Czech guide screenshots the live UI, so one empty msgstr means one
English sentence in a Czech manual.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PO_PATH = REPO_ROOT / "locale/cs/LC_MESSAGES/django.po"


def _unescape_and_concat(parts: list[str]) -> str:
    """Concatenate quoted string parts, unescaping embedded backslash-quotes."""
    result = []
    for part in parts:
        # Remove surrounding quotes
        if part.startswith('"') and part.endswith('"'):
            # Unescape \" to "
            part = part[1:-1].replace('\\"', '"')
        result.append(part)
    return ''.join(result)


def _untranslated(po_text: str) -> list[str]:
    """msgids whose msgstr is empty, ignoring the header entry (msgid "").

    Properly handles wrapped entries: msgid and msgstr may span multiple lines.
    A wrapped translation looks like:
        msgstr ""
        "continuation line"
    This is NOT empty; an empty translation is msgstr "" with no continuation.

    Also handles plural entries (msgid_plural with msgstr[0], msgstr[1], etc.)
    and flags an entry as untranslated if any msgstr[N] is empty.
    """
    untranslated = []
    lines = po_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]

        if line.startswith("msgid "):
            # Accumulate msgid (may be wrapped)
            msgid_parts = [line[len("msgid "):]]
            i += 1
            while i < len(lines) and lines[i].startswith('"'):
                msgid_parts.append(lines[i])
                i += 1

            msgid_value = _unescape_and_concat(msgid_parts)

            # Check for msgid_plural (indicates plural entry)
            is_plural = False
            if i < len(lines) and lines[i].startswith("msgid_plural "):
                is_plural = True
                i += 1
                while i < len(lines) and lines[i].startswith('"'):
                    i += 1

            # Now collect msgstr or msgstr[N] entries
            if is_plural:
                # Plural entry: check all msgstr[N] values
                has_empty = False
                while i < len(lines) and lines[i].startswith("msgstr["):
                    # Extract the msgstr[N] value
                    msgstr_line = lines[i]
                    msgstr_parts = [msgstr_line[msgstr_line.index("]") + 2:]]
                    i += 1
                    while i < len(lines) and lines[i].startswith('"'):
                        msgstr_parts.append(lines[i])
                        i += 1
                    msgstr_value = _unescape_and_concat(msgstr_parts)
                    if msgstr_value == '':
                        has_empty = True
                        break

                if has_empty and msgid_value != '':
                    untranslated.append(msgid_value)
            else:
                # Singular entry: check msgstr
                if i < len(lines) and lines[i].startswith("msgstr "):
                    msgstr_parts = [lines[i][len("msgstr "):]]
                    i += 1
                    while i < len(lines) and lines[i].startswith('"'):
                        msgstr_parts.append(lines[i])
                        i += 1

                    msgstr_value = _unescape_and_concat(msgstr_parts)

                    # Entry is untranslated if msgstr is empty and msgid is not header
                    if msgstr_value == '' and msgid_value != '':
                        untranslated.append(msgid_value)
        else:
            i += 1

    return untranslated


def test_czech_catalog_has_no_untranslated_strings():
    missing = _untranslated(PO_PATH.read_text(encoding="utf-8"))
    assert missing == [], f"Untranslated Czech strings: {missing}"


def test_parser_detects_untranslated_with_wrapped_msgid():
    """Verify the parser detects genuinely empty msgstr on wrapped msgids.

    This is the false-negative case: when msgid spans multiple lines,
    the parser must still detect an empty msgstr.
    """
    po_fragment = '''
#: some/file.html:1
msgid ""
"This is a very long message "
"that wraps across multiple lines"
msgstr ""

#: another/file.html:2
msgid "Another message"
msgstr "Translation"
'''
    missing = _untranslated(po_fragment)
    # Should find the wrapped msgid with empty msgstr
    assert 'This is a very long message that wraps across multiple lines' in missing
    # Should not find the one with a translation
    assert 'Another message' not in missing


def test_parser_handles_wrapped_msgstr():
    """Verify wrapped msgstr (with content) is not flagged as untranslated.

    This is the false-positive case: msgstr "" followed by continuation lines
    is a valid wrapped translation, not empty.
    """
    po_fragment = '''
#: some/file.html:1
msgid "Single line message"
msgstr ""
"This translation wraps "
"across multiple lines"
'''
    missing = _untranslated(po_fragment)
    # Should not flag this as untranslated
    assert missing == []


def test_parser_detects_untranslated_plural_form():
    """Verify the parser detects when a plural form is untranslated.

    Plural entries have msgid_plural and multiple msgstr[N] forms. If any
    msgstr[N] is empty, the entry should be flagged as untranslated.
    """
    po_fragment = '''
#: some/file.html:1
msgid "One item"
msgid_plural "%d items"
msgstr[0] "Jedna položka"
msgstr[1] "%(count)d položky"
msgstr[2] ""

#: another/file.html:2
msgid "Another singular"
msgid_plural "Another plural"
msgstr[0] "Překlad 0"
msgstr[1] "Překlad 1"
msgstr[2] "Překlad 2"
'''
    missing = _untranslated(po_fragment)
    # Should find the plural entry with empty msgstr[2]
    assert 'One item' in missing
    # Should not find the fully translated one
    assert 'Another singular' not in missing
