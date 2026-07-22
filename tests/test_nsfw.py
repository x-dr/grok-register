# -*- coding: utf-8 -*-
"""Offline unit tests for NSFW gRPC framing and birthdate helper."""

from __future__ import annotations

import re
import struct
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from grok_register.nsfw import (
    NSFW_FEATURE_KEY,
    encode_set_tos_accepted_body,
    encode_update_nsfw_body,
    generate_random_birthdate,
)


def _mytest_encode_set_tos() -> bytes:
    payload = struct.pack("B", (2 << 3) | 0) + struct.pack("B", 1)
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def _mytest_encode_nsfw() -> bytes:
    field1_content = bytes([0x10, 0x01])
    field1 = bytes([0x0A, len(field1_content)]) + field1_content
    nsfw_string = NSFW_FEATURE_KEY.encode("utf-8")
    field2_inner = bytes([0x0A, len(nsfw_string)]) + nsfw_string
    field2 = bytes([0x12, len(field2_inner)]) + field2_inner
    payload = field1 + field2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


class NsfwEncodingTests(unittest.TestCase):
    def test_set_tos_matches_mytest(self) -> None:
        self.assertEqual(encode_set_tos_accepted_body(), _mytest_encode_set_tos())

    def test_update_nsfw_matches_mytest(self) -> None:
        self.assertEqual(encode_update_nsfw_body(), _mytest_encode_nsfw())

    def test_update_nsfw_contains_feature_key(self) -> None:
        body = encode_update_nsfw_body()
        self.assertIn(NSFW_FEATURE_KEY.encode("utf-8"), body)
        self.assertTrue(body.startswith(b"\x00"))
        length = struct.unpack(">I", body[1:5])[0]
        self.assertEqual(len(body), 5 + length)

    def test_birthdate_format_and_age(self) -> None:
        pat = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T16:00:00\.000Z$")
        for _ in range(20):
            s = generate_random_birthdate()
            m = pat.match(s)
            self.assertIsNotNone(m, s)
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            self.assertTrue(1 <= month <= 12)
            self.assertTrue(1 <= day <= 28)
            from datetime import date

            age = date.today().year - year
            self.assertTrue(20 <= age <= 40)


if __name__ == "__main__":
    unittest.main()
