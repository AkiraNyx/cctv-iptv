from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main

from check import fetch, identify, playlist, read_baseline


class PlaylistTests(TestCase):
    def test_snapshot_has_twenty_channels_and_matches_its_metadata(self):
        entries, baseline, _ = read_baseline()
        self.assertEqual(len(entries), 20)
        self.assertEqual(set(dict(entries)), set(baseline["channels"]))

    def test_channel_aliases_and_exclusions(self):
        for name, expected in (
            ("CCTV5＋ 体育赛事", "CCTV-5+"),
            ("CCTV4K", "CCTV-4K"),
            ("CCTV-16 奥林匹克 ４Ｋ", "CCTV-16-4K"),
            ("CCTV-16 奥林匹克", "CCTV-16"),
            ("CCTV-4 美洲", None),
            ("CCTV-8K", None),
            ("CCTV-18", None),
        ):
            with self.subTest(name=name):
                self.assertEqual(identify(name), expected)

    def test_comments_and_quoted_commas_do_not_change_entries(self):
        text = '#EXTM3U\n#EXTINF:-1 group-title="央视频道,体育",CCTV-5+\n# comment\nhttps://example.com/live\n'
        self.assertEqual(list(playlist(text, strict=True)), [("CCTV-5+", "https://example.com/live")])

    def test_malformed_headers_and_orphans_fail(self):
        for text in ("not a playlist", "#EXTM3U\n#EXTINF:-1,CCTV-1", "#EXTM3U\nhttps://example.com"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                list(playlist(text, strict=True))

    def test_nonpublic_stream_url_is_rejected_before_fetch(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "no public IP"):
                fetch("http://127.0.0.1", Path(directory) / "sample", 100)


if __name__ == "__main__":
    main()
