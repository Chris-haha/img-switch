"""Exact configured credentials must never survive error/log sanitization."""
import json
import re
from pathlib import Path
import sys
import unittest
import urllib.parse
import tempfile

sys.path.insert(0, str(Path(__file__).parent / "engine/scripts"))
import failovercfg


class ExactRedactionTests(unittest.TestCase):
    def test_url_encoded_unreserved_characters_and_mixed_encoding(self):
        secret = 'Relay Token/+秘密'
        encoded = ''.join('%%%02x' % byte for byte in secret.encode())
        mixed = '%52elay+Token%2f%2B' + urllib.parse.quote('秘密')
        for value in (encoded, mixed):
            self.assertEqual(failovercfg.mask_secrets(value, [secret]), '<redacted>')

    def test_url_escape_hex_is_case_insensitive_but_token_is_not(self):
        secret = 'Relay-Token/+秘密="Value"'
        encoded = urllib.parse.quote(secret, safe="")
        lower = re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), encoded)
        mixed = lower.replace("%e7", "%E7").replace("%2f", "%2F")
        for value in (lower, mixed):
            self.assertEqual(failovercfg.mask_secrets(value, [secret]), "<redacted>")
        self.assertEqual(failovercfg.mask_secrets(secret.swapcase(), [secret]), secret.swapcase())

    def test_nonstandard_and_encoded_credentials(self):
        secret = 'relay-token/+秘密="value"'
        values = [secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
                  json.dumps(secret)[1:-1], json.dumps(secret, ensure_ascii=False)[1:-1]]
        for value in values:
            with self.subTest(value=value):
                text = failovercfg.mask_secrets("Rejected credential: " + value, [secret])
                self.assertNotIn(value, text)
                self.assertIn("<redacted>", text)

    def test_recursive_record_scrubbing_keeps_numbers_and_original(self):
        secret = "unprefixed_credential_4321"
        raw = {"error": secret, "attempts": [{"message": "token=" + secret, "ok": False}], "duration": 1.2}
        cleaned = failovercfg.redact_data(raw, [secret])
        self.assertNotIn(secret, json.dumps(cleaned))
        self.assertEqual(cleaned["duration"], 1.2)
        self.assertFalse(cleaned["attempts"][0]["ok"])
        self.assertEqual(raw["error"], secret)

    def test_short_and_overlapping_credentials(self):
        cleaned = failovercfg.mask_secrets("ab abcdefgh", ["ab", "abcdefgh", ""])
        self.assertEqual(cleaned, "<redacted> <redacted>")

    def test_standard_patterns_remain_supported(self):
        cleaned = failovercfg.mask_secrets("sk-123456789 Bearer qwertyuio")
        self.assertNotIn("123456789", cleaned)
        self.assertNotIn("qwertyuio", cleaned)


class CapabilityConfigTests(unittest.TestCase):
    def test_invalid_capability_lists_are_rejected_before_requests(self):
        valid = {"name": "a", "base_url": "http://localhost/v1", "api_key": "fake", "model": "image"}
        for field, value in (("supported_sizes", "1024x1024"), ("supported_sizes", ["bad"]),
                             ("supported_sizes", ["0x64"]), ("supported_models", [123]),
                             ("supported_models", [])):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps({"sources": [dict(valid, **{field: value})]}))
                with self.assertRaises(RuntimeError):
                    failovercfg.load_config(str(path))

    def test_capability_limits_survive_load(self):
        source = {"name": "a", "base_url": "http://localhost/v1", "api_key": "fake", "model": "image",
                  "supported_sizes": ["1024x1024", "2k"], "supported_models": ["image"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"sources": [source]}))
            self.assertEqual(failovercfg.load_config(str(path))["sources"][0], source)


if __name__ == "__main__":
    unittest.main()
