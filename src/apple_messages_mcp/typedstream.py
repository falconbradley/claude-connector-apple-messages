"""
Decoder for Messages' ``attributedBody`` column.

Since macOS 13 Ventura, ``message.text`` is frequently NULL and the real body
lives in ``message.attributedBody`` as an Apple *typedstream* — the legacy
``NSArchiver`` format, not ``NSKeyedArchiver``.  ``plistlib`` cannot read it.

We decode it without PyObjC so the extension stays a pure-Python bundle.

Format notes
------------
A typedstream begins with::

    \\x04\\x0b streamtyped \\x81\\xe8\\x03

then a sequence of typed values.  Integers are variable-width:

    0x81  -> next 2 bytes, little-endian uint16
    0x82  -> next 4 bytes, little-endian uint32
    0x83  -> next 8 bytes, little-endian uint64
    other -> the byte itself (signed)

An ``NSAttributedString`` archive stores its backing store as an ``NSString``
whose bytes follow a ``+`` type marker, length-prefixed with the encoding
above.  The message body is the first such string in the archive; everything
after it is attribute-run metadata (link ranges, mention spans, effects).
"""

from __future__ import annotations

import logging

logger = logging.getLogger("apple_messages_mcp.typedstream")

# Marker that introduces a raw byte-string payload in a typedstream.
_BYTES_MARKER = b"+"

# Class names whose instance carries the attributed string's characters.
_STRING_CLASSES = (b"NSMutableString", b"NSString")

# U+FFFC OBJECT REPLACEMENT CHARACTER — placeholder Messages inserts where an
# attachment, inline image, or rich-link preview sits in the text run.
OBJECT_REPLACEMENT = "￼"


def _read_varint(data: bytes, i: int) -> tuple[int, int]:
    """Read a typedstream variable-width integer at ``i``.

    Returns ``(value, next_index)``.
    """
    if i >= len(data):
        raise ValueError("truncated varint")
    b = data[i]
    if b == 0x81:
        return int.from_bytes(data[i + 1 : i + 3], "little"), i + 3
    if b == 0x82:
        return int.from_bytes(data[i + 1 : i + 5], "little"), i + 5
    if b == 0x83:
        return int.from_bytes(data[i + 1 : i + 9], "little"), i + 9
    return b, i + 1


def _extract_at(data: bytes, start: int) -> str | None:
    """Extract the length-prefixed UTF-8 payload following ``start``."""
    marker = data.find(_BYTES_MARKER, start)
    if marker == -1:
        return None
    try:
        length, body_start = _read_varint(data, marker + 1)
    except ValueError:
        return None
    if length <= 0 or body_start + length > len(data):
        return None
    return data[body_start : body_start + length].decode("utf-8", errors="replace")


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Return the plain text carried by an ``attributedBody`` blob.

    Returns ``None`` when the blob is empty or no string payload is found.
    Never raises — a body we cannot decode must not break a whole query.
    """
    if not blob:
        return None

    try:
        # The characters live in the archive's first NSString instance.  Anchor
        # on the class name so we skip the header and any leading class table.
        for class_name in _STRING_CLASSES:
            idx = blob.find(class_name)
            if idx == -1:
                continue
            text = _extract_at(blob, idx + len(class_name))
            if text is not None:
                return text

        # No class-name anchor (rare, e.g. a re-used class reference). Fall back
        # to the first plausible payload anywhere in the stream.
        return _extract_at(blob, 0)
    except Exception:  # pragma: no cover — decoding must be total
        logger.debug("attributedBody decode failed", exc_info=True)
        return None


def message_text(text: str | None, attributed_body: bytes | None) -> str | None:
    """Best available body for a row, preferring the plain ``text`` column."""
    if text:
        return text
    return decode_attributed_body(attributed_body)
