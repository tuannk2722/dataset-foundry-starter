import io
import unittest

from dataset_foundry.content_types import (
    MAX_PREFIX_BYTES,
    mime_allowed,
    mime_types_compatible,
    normalize_mime,
    read_bounded_prefix,
    sniff_mime,
)


class ContentTypeTests(unittest.TestCase):
    def test_sniffs_common_multimodal_signatures(self):
        samples = {
            b"\x89PNG\r\n\x1a\nrest": "image/png",
            b"\xff\xd8\xff\xe0rest": "image/jpeg",
            b"RIFF\x00\x00\x00\x00WEBPrest": "image/webp",
            b"RIFF\x00\x00\x00\x00WAVErest": "audio/wav",
            b"\x00\x00\x00\x18ftypisomrest": "video/mp4",
            b"%PDF-1.7\n": "application/pdf",
            b"PK\x03\x04rest": "application/zip",
            b"plain utf-8 text\n": "text/plain",
            b'{"key": "value"}': "application/json",
        }
        for content, expected in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(sniff_mime(content), expected)
        self.assertIsNone(sniff_mime(b"\x00\x01\x02unknown"))

    def test_prefix_read_is_bounded_and_requires_binary_stream(self):
        stream = io.BytesIO(b"0123456789")
        self.assertEqual(read_bounded_prefix(stream, 4), b"0123")
        self.assertEqual(stream.tell(), 4)
        with self.assertRaises(ValueError):
            read_bounded_prefix(stream, MAX_PREFIX_BYTES + 1)
        with self.assertRaises(TypeError):
            read_bounded_prefix(io.StringIO("text"), 4)  # type: ignore[arg-type]

    def test_mime_normalization_compatibility_and_allow_list(self):
        self.assertEqual(normalize_mime(" Image/JPG; charset=binary "), "image/jpeg")
        self.assertTrue(mime_types_compatible("image/*", "image/png"))
        self.assertTrue(mime_types_compatible("application/octet-stream", "video/mp4"))
        self.assertFalse(mime_types_compatible("image/png", "image/jpeg"))
        self.assertTrue(mime_types_compatible("text/csv", "text/plain"))
        self.assertTrue(mime_allowed("image/png", ["image/*", "application/pdf"]))
        self.assertFalse(mime_allowed("audio/mpeg", ["image/*"]))
        self.assertFalse(mime_allowed(None, ["image/*"]))
        self.assertTrue(mime_allowed(None, []))
        self.assertTrue(mime_allowed("video/mp4", ["*/*"]))


if __name__ == "__main__":
    unittest.main()
