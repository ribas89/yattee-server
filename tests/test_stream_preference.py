"""Regression tests for progressive video+audio preference.

Run standalone: python -m unittest discover -s tests -p test_stream_preference.py -v
"""
import unittest
from unittest.mock import patch

import config
from models import AdaptiveFormat, FormatStream, VideoResponse
from stream_preference import prefer_combined_streams


def response(muxed=(), adaptive=(), live=False):
    return VideoResponse(
        videoId="wh6dlpO-f3A", title="Test", author="A", authorId="UCxxx",
        lengthSeconds=1177, liveNow=live,
        formatStreams=list(muxed), adaptiveFormats=list(adaptive),
    )


def fmt(itag, type="video/mp4", container="mp4", url=True):
    return FormatStream(
        url=f"https://example.com/{itag}" if url else "", itag=itag,
        type=type, quality="360p", container=container, height=360,
    )


def adaptive(itag, type="video/mp4"):
    return AdaptiveFormat(url=f"https://example.com/{itag}", itag=itag,
                          type=type, container="mp4", height=360 if type.startswith("video/") else None)


class StreamPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.preference = patch.object(config, "PREFER_COMBINED_STREAMS", True)
        self.preference.start()
        self.addCleanup(self.preference.stop)

    def test_360p_progressive_wins_over_360p_video_only(self):
        r = response([fmt("18")], [adaptive("134"), adaptive("140-drc", "audio/mp4")])
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.formatStreams], ["18"])
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["140-drc"])

    def test_disable_restores_adaptive_video(self):
        with patch.object(config, "PREFER_COMBINED_STREAMS", False):
            r = response([fmt("18")], [adaptive("134"), adaptive("140", "audio/mp4")])
            prefer_combined_streams(r)
            self.assertEqual([f.itag for f in r.adaptiveFormats], ["134", "140"])

    def test_no_muxed_format_preserves_adaptive(self):
        r = response([], [adaptive("134"), adaptive("140", "audio/mp4")])
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["134", "140"])

    def test_hls_only_preserves_adaptive(self):
        r = response([fmt("hls", "application/vnd.apple.mpegurl", "hls")],
                     [adaptive("134"), adaptive("140", "audio/mp4")])
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["134", "140"])

    def test_unplayable_muxed_url_does_not_hide_adaptive(self):
        r = response([fmt("18", url=False)], [adaptive("134")])
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["134"])

    def test_live_preserves_adaptive(self):
        r = response([fmt("18")], [adaptive("134")], live=True)
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["134"])

    def test_itag18_first_when_multiple_muxed(self):
        r = response([fmt("22"), fmt("18"), fmt("34")], [adaptive("134")])
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.formatStreams], ["18", "22", "34"])
        self.assertEqual(r.adaptiveFormats, [])

    def test_audio_tracks_preserved_even_with_multiple_muxed(self):
        r = response([fmt("18"), fmt("22")],
                     [adaptive("134"), adaptive("140-drc", "audio/mp4"),
                      adaptive("251", "audio/webm")])
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["140-drc", "251"])

    def test_does_not_mutate_stream_urls(self):
        r = response([fmt("18")], [adaptive("134")])
        before = r.formatStreams[0].url
        prefer_combined_streams(r)
        self.assertEqual(r.formatStreams[0].url, before)


if __name__ == "__main__":
    unittest.main()

# These checks use the same model format converters as /api/v1/videos, without
# involving auth or a running database.
class HybridCandidateTests(unittest.TestCase):
    def setUp(self):
        self.preference = patch.object(config, "PREFER_COMBINED_STREAMS", True)
        self.preference.start()
        self.addCleanup(self.preference.stop)

    def test_itag18_recovered_from_yt_dlp_when_innertube_omits_it(self):
        from stream_preference import add_combined_candidate
        r = response([], [adaptive("134"), adaptive("140", "audio/mp4")])
        extracted = response([fmt("18")])
        add_combined_candidate(r, extracted)
        prefer_combined_streams(r)
        self.assertEqual([f.itag for f in r.formatStreams], ["18"])
        self.assertEqual([f.itag for f in r.adaptiveFormats], ["140"])

    def test_do_not_duplicate_existing_itag18(self):
        from stream_preference import add_combined_candidate
        r = response([fmt("18")])
        add_combined_candidate(r, response([fmt("18")]))
        self.assertEqual([f.itag for f in r.formatStreams], ["18"])

    def test_recover_other_muxed_only_when_none_available(self):
        from stream_preference import add_combined_candidate
        r = response([], [adaptive("134")])
        add_combined_candidate(r, response([fmt("22")]))
        self.assertEqual([f.itag for f in r.formatStreams], ["22"])

    def test_no_restore_when_setting_disabled(self):
        from stream_preference import add_combined_candidate
        r = response([], [adaptive("134")])
        with patch.object(config, "PREFER_COMBINED_STREAMS", False):
            add_combined_candidate(r, response([fmt("18")]))
        self.assertEqual(r.formatStreams, [])
