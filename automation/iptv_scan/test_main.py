import unittest

from main import parse_playable_channels


class ParsePlayableChannelsTests(unittest.TestCase):
    def test_current_summary_format(self):
        self.assertEqual(
            parse_playable_channels(
                "12 kênh · 40 link · Đã test 40/40 · Kênh phát được: 7 (xác minh: 5)"
            ),
            7,
        )

    def test_legacy_summary_format(self):
        self.assertEqual(parse_playable_channels("PHÁT ĐƯỢC: 3 kênh"), 3)

    def test_case_and_spacing_are_ignored(self):
        self.assertEqual(parse_playable_channels("KÊNH   PHÁT ĐƯỢC : 11"), 11)

    def test_unrecognized_summary_fails_loudly(self):
        with self.assertRaises(ValueError):
            parse_playable_channels("Quét xong nhưng không có số liệu")


if __name__ == "__main__":
    unittest.main()
