# -*- coding: utf-8 -*-
"""Offline unit tests for 22.do email channel helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from grok_register.email_22do import (
    _decode_cf_email,
    _parse_inbox,
    _text,
    extract_xai_code,
)


class ExtractCodeTests(unittest.TestCase):
    def test_dashed_code_in_subject(self) -> None:
        self.assertEqual(
            extract_xai_code("body", subject="LSQ-OPU xAI"),
            "LSQ-OPU",
        )

    def test_dashed_code_in_body(self) -> None:
        self.assertEqual(
            extract_xai_code("Your verification code is ABC-DEF"),
            "ABC-DEF",
        )

    def test_legacy_six_char(self) -> None:
        self.assertEqual(extract_xai_code("code: XAI0X1"), "XAI0X1")

    def test_digit_fallback(self) -> None:
        self.assertEqual(extract_xai_code("Your code is 123456"), "123456")


class HtmlParseTests(unittest.TestCase):
    def test_decode_cf_email(self) -> None:
        # Cloudflare email obfuscation: key 0x0a, then xor'd bytes for a@b.c
        # Use empty / short guard
        self.assertEqual(_decode_cf_email(""), "")
        self.assertEqual(_decode_cf_email("0"), "")

    def test_text_strips_tags(self) -> None:
        self.assertEqual(_text("<b>Hello</b> &amp; world"), "Hello & world")

    def test_parse_inbox_row(self) -> None:
        html = (
            '<div class="tr">'
            '<div class="item subject" onclick="viewEml(\'mid123\')">Subj</div>'
            '<div class="item from">from@x.ai</div>'
            '<div class="item time receive-time" data-bs-time="1700000000">now</div>'
            "</div>"
        )
        msgs = _parse_inbox(html)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["message_id"], "mid123")
        self.assertEqual(msgs[0]["subject"], "Subj")
        self.assertEqual(msgs[0]["from"], "from@x.ai")
        self.assertEqual(msgs[0]["timestamp"], 1700000000)


if __name__ == "__main__":
    unittest.main()
