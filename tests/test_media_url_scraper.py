import unittest

import media_url_scraper as scraper


class MediaClassificationTests(unittest.TestCase):
    def test_video_content_types_include_extensionless_media(self):
        self.assertEqual(scraper.kind_from_content_type("video/mp4", "https://cdn.test/play?id=1"), "video")
        self.assertEqual(scraper.kind_from_content_type("video/mp2t", "https://cdn.test/chunk?id=1"), "video")
        self.assertEqual(
            scraper.kind_from_content_type("application/octet-stream", "https://cdn.test/play?id=1", "media"),
            "video",
        )

    def test_network_content_type_reclassifies_extensionless_dom_url(self):
        state = scraper.MediaState()
        url = "https://cdn.test/play?id=1"
        state.add({"url": url, "kind": "image", "source": "dom"})
        state.add({"url": url, "kind": "video", "source": "network", "content_type": "video/mp4"})
        self.assertEqual(len(state.all()), 1)
        self.assertEqual(state.all()[0]["kind"], "video")


class LongGalleryDefaultsTests(unittest.TestCase):
    def test_defaults_cover_more_than_3000_items_in_40_item_batches(self):
        cfg = scraper.Config()
        self.assertGreaterEqual(cfg.max_load_more_clicks, 75)
        self.assertGreaterEqual(cfg.max_scroll_rounds, 75)

    def test_max_load_more_can_be_set_from_command_line(self):
        args = scraper.parse_args(["--max-load-more", "90", "--max-scroll-rounds", "100"])
        cfg = scraper.build_config(args)
        self.assertEqual(cfg.max_load_more_clicks, 90)
        self.assertEqual(cfg.max_scroll_rounds, 100)


if __name__ == "__main__":
    unittest.main()
