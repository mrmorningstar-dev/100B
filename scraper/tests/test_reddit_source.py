import os
import unittest
from unittest.mock import MagicMock, patch
from sources.reddit import RedditSource
from sources.base import VideoCandidate


class TestRedditSource(unittest.TestCase):
    def setUp(self):
        self.config = {
            "reddit": {
                "subreddits": ["funny"],
                "listings": ["hot", "top_day"],
                "limit_per_sub": 10,
                "rate_limit_delay": [0.0, 0.0],
            }
        }

    @patch("sources.reddit.os.getenv")
    def test_init_praw_ignores_placeholders(self, mock_getenv):
        mock_getenv.side_effect = lambda k, d=None: {
            "REDDIT_CLIENT_ID": "your_reddit_client_id_here",
            "REDDIT_CLIENT_SECRET": "your_reddit_client_secret_here",
        }.get(k, d)

        source = RedditSource(self.config)
        self.assertIsNone(source.praw_client)

    @patch.object(RedditSource, "_init_praw")
    @patch.object(RedditSource, "_fetch_via_praw")
    @patch.object(RedditSource, "_fetch_via_json")
    @patch.object(RedditSource, "_fetch_via_rss")
    def test_layer_order_praw_first_if_configured(
        self, mock_rss, mock_json, mock_praw, mock_init
    ):
        mock_init.return_value = None
        source = RedditSource(self.config)
        source.praw_client = MagicMock()

        # PRAW returns full target
        cand = VideoCandidate(
            source="reddit",
            post_id="reddit_p1",
            post_url="https://reddit.com/p1",
            title="PRAW Video",
            uploader="user1",
            score=10000,
            upvote_ratio=0.98,
            num_comments=500,
            created_utc=1234567.0,
            video_url="https://v.redd.it/p1",
            is_nsfw=False,
        )
        mock_praw.return_value = [cand]

        result = source.fetch_candidates(target_count=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].post_id, "reddit_p1")
        mock_praw.assert_called_once()
        mock_json.assert_not_called()
        mock_rss.assert_not_called()

    @patch.object(RedditSource, "_init_praw")
    @patch.object(RedditSource, "_fetch_via_praw")
    @patch.object(RedditSource, "_fetch_via_json")
    @patch.object(RedditSource, "_fetch_via_rss")
    def test_layer_order_json_before_rss_when_no_praw(
        self, mock_rss, mock_json, mock_praw, mock_init
    ):
        mock_init.return_value = None
        source = RedditSource(self.config)
        source.praw_client = None

        cand_json = VideoCandidate(
            source="reddit",
            post_id="reddit_j1",
            post_url="https://reddit.com/j1",
            title="JSON Video",
            uploader="user2",
            score=5000,
            upvote_ratio=0.95,
            num_comments=250,
            created_utc=1234567.0,
            video_url="https://v.redd.it/j1",
            is_nsfw=False,
        )
        mock_json.return_value = [cand_json]

        # Target = 1 satisfied by JSON
        result = source.fetch_candidates(target_count=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].post_id, "reddit_j1")
        mock_praw.assert_not_called()
        mock_json.assert_called_once()
        # RSS should not be called if JSON satisfies target
        mock_rss.assert_not_called()

    @patch.object(RedditSource, "_init_praw")
    @patch.object(RedditSource, "_fetch_via_praw")
    @patch.object(RedditSource, "_fetch_via_json")
    @patch.object(RedditSource, "_fetch_via_rss")
    def test_rss_called_as_fallback_when_json_insufficient(
        self, mock_rss, mock_json, mock_praw, mock_init
    ):
        mock_init.return_value = None
        source = RedditSource(self.config)
        source.praw_client = None

        # JSON returns 0 candidates (e.g. rate limit / 403)
        mock_json.return_value = []

        cand_rss = VideoCandidate(
            source="reddit",
            post_id="reddit_r1",
            post_url="https://reddit.com/r1",
            title="RSS Video",
            uploader="user3",
            score=8000,
            upvote_ratio=0.95,
            num_comments=300,
            created_utc=1234567.0,
            video_url="https://v.redd.it/r1",
            is_nsfw=False,
        )
        mock_rss.return_value = [cand_rss]

        result = source.fetch_candidates(target_count=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].post_id, "reddit_r1")
        mock_json.assert_called_once()
        mock_rss.assert_called_once()

    def test_fetch_via_json_parses_real_engagement_and_filters_non_videos(self):
        source = RedditSource(self.config)
        fake_json_payload = {
            "data": {
                "children": [
                    {
                        "data": {
                            "id": "vid1",
                            "permalink": "/r/funny/comments/vid1/funny_dog/",
                            "title": "Funny Dog Running",
                            "author": "dog_lover",
                            "score": 12450,
                            "upvote_ratio": 0.97,
                            "num_comments": 432,
                            "created_utc": 1600000000.0,
                            "is_video": True,
                            "domain": "v.redd.it",
                            "media": {
                                "reddit_video": {
                                    "duration": 15.5,
                                    "fallback_url": "https://v.redd.it/vid1/DASH_720.mp4",
                                }
                            },
                            "over_18": False,
                        }
                    },
                    {
                        # Non-video post (image/text with word video in title)
                        "data": {
                            "id": "img1",
                            "permalink": "/r/funny/comments/img1/funny_video_meme/",
                            "title": "A meme about video games",
                            "author": "gamer",
                            "score": 25000,
                            "upvote_ratio": 0.99,
                            "num_comments": 1200,
                            "created_utc": 1600000100.0,
                            "is_video": False,
                            "domain": "i.redd.it",
                            "media": None,
                            "over_18": False,
                        }
                    },
                ]
            }
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_json_payload
        source.http.get = MagicMock(return_value=mock_resp)

        seen_ids = set()
        candidates = source._fetch_via_json(target_count=5, seen_ids=seen_ids)

        # Only 1 video candidate accepted, image post ignored
        self.assertEqual(len(candidates), 1)
        c = candidates[0]
        self.assertEqual(c.post_id, "reddit_vid1")
        self.assertEqual(c.score, 12450)
        self.assertEqual(c.num_comments, 432)
        self.assertEqual(c.upvote_ratio, 0.97)
        self.assertEqual(c.duration, 15.5)
        # Verify video_url is the post link or v.redd.it link, NOT the video-only fallback_url
        self.assertEqual(c.video_url, "https://www.reddit.com/r/funny/comments/vid1/funny_dog/")
        self.assertNotEqual(c.video_url, "https://v.redd.it/vid1/DASH_720.mp4")
        self.assertEqual(c.extra.get("fallback_url"), "https://v.redd.it/vid1/DASH_720.mp4")
        self.assertIn("vid1", seen_ids)

    def test_fetch_via_rss_rejects_text_posts_with_word_video(self):
        source = RedditSource(self.config)
        # Atom feed with:
        # 1. Non-video entry mentioning "video" in text
        # 2. Genuine video entry with v.redd.it link
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>My favorite video game memory</title>
            <link href="https://www.reddit.com/r/funny/comments/text1/my_favorite_video_game/"/>
            <author><name>/u/author1</name></author>
            <content type="html">&lt;p&gt;This is a text discussion about a video game.&lt;/p&gt;</content>
          </entry>
          <entry>
            <title>Cat jumps over sofa</title>
            <link href="https://www.reddit.com/r/funny/comments/vid2/cat_jumps_over_sofa/"/>
            <author><name>/u/cat_fan</name></author>
            <content type="html">&lt;span&gt;&lt;a href="https://v.redd.it/cat123"&gt;[link]&lt;/a&gt;&lt;/span&gt;</content>
          </entry>
        </feed>"""

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = rss_xml
        source.http.get = MagicMock(return_value=mock_resp)

        seen_ids = set()
        candidates = source._fetch_via_rss(target_count=5, seen_ids=seen_ids)

        # text post with word "video" MUST be rejected!
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].post_id, "reddit_vid2")
        self.assertEqual(candidates[0].video_url, "https://v.redd.it/cat123")
        # Ensure engagement is not flat 500
        self.assertGreater(candidates[0].score, 500)
        self.assertTrue(candidates[0].extra.get("is_estimated_score"))

    def test_fetch_via_rss_rank_decay_differentiates_engagement(self):
        source = RedditSource(self.config)
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <title>Top video 1</title>
            <link href="https://www.reddit.com/r/funny/comments/v1/top_video_1/"/>
            <content type="html">&lt;a href="https://v.redd.it/v1"&gt;[link]&lt;/a&gt;</content>
          </entry>
          <entry>
            <title>Top video 2</title>
            <link href="https://www.reddit.com/r/funny/comments/v2/top_video_2/"/>
            <content type="html">&lt;a href="https://v.redd.it/v2"&gt;[link]&lt;/a&gt;</content>
          </entry>
        </feed>"""

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = rss_xml
        source.http.get = MagicMock(return_value=mock_resp)

        seen_ids = set()
        candidates = source._fetch_via_rss(target_count=5, seen_ids=seen_ids)

        self.assertEqual(len(candidates), 2)
        # Entry 0 has higher score and comments than Entry 1
        self.assertGreater(candidates[0].score, candidates[1].score)
        self.assertGreater(candidates[0].num_comments, candidates[1].num_comments)

    def test_resolve_cookies_file_for_reddit(self):
        from core.downloader import resolve_cookies_file
        # Test reddit config priority
        cfg = {"reddit": {"cookies_file": "reddit_cookies.txt"}}
        self.assertEqual(resolve_cookies_file("reddit", cfg), "reddit_cookies.txt")

        # Test global cookies_file fallback
        cfg_global = {"cookies_file": "shared_cookies.txt"}
        self.assertEqual(resolve_cookies_file("reddit", cfg_global), "shared_cookies.txt")

    @patch("core.downloader.subprocess.run")
    def test_download_reddit_with_ffmpeg_hls(self, mock_subproc):
        from core.downloader import download_reddit_with_ffmpeg
        from pathlib import Path

        mock_subproc.return_value = MagicMock(returncode=0)
        cand = MagicMock()
        cand.video_url = "https://v.redd.it/test123"
        cand.post_url = "https://reddit.com/r/test"
        cand.extra = {}

        with patch("pathlib.Path.is_file", return_value=True), patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value.st_size = 50000
            result = download_reddit_with_ffmpeg(cand, Path("temp_out"))
            self.assertTrue(result)
            mock_subproc.assert_called_once()
            cmd_called = mock_subproc.call_args[0][0]
            self.assertIn("https://v.redd.it/test123/HLSPlaylist.m3u8", cmd_called)
            self.assertIn("-c", cmd_called)
            self.assertIn("copy", cmd_called)

    def test_pagination_cursor_saved_and_reused_in_top(self):
        import tempfile
        import sqlite3
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "agent.db"
            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute("""
                    CREATE TABLE sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        listing_mode TEXT NOT NULL,
                        status TEXT DEFAULT 'active',
                        pagination_cursor TEXT DEFAULT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    INSERT INTO sources (source_type, name, listing_mode, status, pagination_cursor)
                    VALUES ('subreddit', 'funny', 'top_week', 'active', 't3_prev123')
                """)
            conn.close()

            config = {
                "reddit": {
                    "subreddits": ["funny"],
                    "listings": ["top_week"],
                    "limit_per_sub": 10,
                    "max_pages_per_source_per_run": 2,
                    "rate_limit_delay": [0.0, 0.0],
                    "custom_sources": [{"type": "subreddit", "name": "funny", "listing": "top_week", "cursor": "t3_prev123"}],
                },
                "registry": {"db_path": str(db_path)},
            }
            source = RedditSource(config)
            source.praw_client = None

            # First page mock: returns 1 video and after="t3_next456"
            resp1 = MagicMock()
            resp1.status_code = 200
            resp1.json.return_value = {
                "data": {
                    "after": "t3_next456",
                    "children": [
                        {
                            "data": {
                                "id": "p1",
                                "name": "t3_p1",
                                "is_video": True,
                                "domain": "v.redd.it",
                                "title": "Top Week Post 1",
                                "author": "u1",
                                "score": 1000,
                                "permalink": "/r/funny/comments/p1/test",
                                "media": {"reddit_video": {"duration": 20, "fallback_url": "https://v.redd.it/p1/DASH_720.mp4"}},
                            }
                        }
                    ]
                }
            }

            # Second page mock: returns 1 video and after="t3_next789"
            resp2 = MagicMock()
            resp2.status_code = 200
            resp2.json.return_value = {
                "data": {
                    "after": "t3_next789",
                    "children": [
                        {
                            "data": {
                                "id": "p2",
                                "name": "t3_p2",
                                "is_video": True,
                                "domain": "v.redd.it",
                                "title": "Top Week Post 2",
                                "author": "u2",
                                "score": 900,
                                "permalink": "/r/funny/comments/p2/test",
                                "media": {"reddit_video": {"duration": 25, "fallback_url": "https://v.redd.it/p2/DASH_720.mp4"}},
                            }
                        }
                    ]
                }
            }

            source.http.get = MagicMock(side_effect=[resp1, resp2])
            seen_ids = set()
            candidates = source._fetch_via_json(target_count=5, seen_ids=seen_ids)

            # Both pages should have been fetched
            self.assertEqual(len(candidates), 2)
            self.assertEqual(source.http.get.call_count, 2)

            # First call should have after='t3_prev123'
            first_call_params = source.http.get.call_args_list[0][1]["params"]
            self.assertEqual(first_call_params.get("after"), "t3_prev123")
            self.assertEqual(first_call_params.get("limit"), 100)

            # Second call should have after='t3_next456'
            second_call_params = source.http.get.call_args_list[1][1]["params"]
            self.assertEqual(second_call_params.get("after"), "t3_next456")

            # Registry DB should have saved the latest cursor 't3_next789'
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute("SELECT pagination_cursor FROM sources WHERE name='funny' AND listing_mode='top_week'")
            saved_cursor = cur.fetchone()[0]
            conn.close()
            self.assertEqual(saved_cursor, "t3_next789")

    def test_pagination_no_cursor_for_hot(self):
        import tempfile
        import sqlite3
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "agent.db"
            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute("""
                    CREATE TABLE sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        listing_mode TEXT NOT NULL,
                        status TEXT DEFAULT 'active',
                        pagination_cursor TEXT DEFAULT 't3_should_not_be_used',
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    INSERT INTO sources (source_type, name, listing_mode, status, pagination_cursor)
                    VALUES ('subreddit', 'funny', 'hot', 'active', 't3_should_not_be_used')
                """)
            conn.close()

            config = {
                "reddit": {
                    "subreddits": ["funny"],
                    "listings": ["hot"],
                    "limit_per_sub": 10,
                    "rate_limit_delay": [0.0, 0.0],
                    "custom_sources": [{"type": "subreddit", "name": "funny", "listing": "hot", "cursor": "t3_ignore_me"}],
                },
                "registry": {"db_path": str(db_path)},
            }
            source = RedditSource(config)
            source.praw_client = None

            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"data": {"children": []}}
            source.http.get = MagicMock(return_value=resp)

            seen_ids = set()
            source._fetch_via_json(target_count=2, seen_ids=seen_ids)

            # In hot mode, params must NEVER include 'after'
            call_params = source.http.get.call_args[1]["params"]
            self.assertNotIn("after", call_params)

    def test_pagination_exhausted_status_after_3_empty_pages(self):
        import tempfile
        import sqlite3
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "agent.db"
            conn = sqlite3.connect(str(db_path))
            with conn:
                conn.execute("""
                    CREATE TABLE sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        listing_mode TEXT NOT NULL,
                        status TEXT DEFAULT 'active',
                        pagination_cursor TEXT DEFAULT NULL,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    INSERT INTO sources (source_type, name, listing_mode, status)
                    VALUES ('subreddit', 'funny', 'top_month', 'active')
                """)
            conn.close()

            config = {
                "reddit": {
                    "subreddits": ["funny"],
                    "listings": ["top_month"],
                    "limit_per_sub": 10,
                    "max_pages_per_source_per_run": 5,
                    "rate_limit_delay": [0.0, 0.0],
                    "custom_sources": [{"type": "subreddit", "name": "funny", "listing": "top_month"}],
                },
                "registry": {"db_path": str(db_path)},
            }
            source = RedditSource(config)
            source.praw_client = None

            # Empty response with cursor to trigger consecutive empty pages
            empty_resp = MagicMock()
            empty_resp.status_code = 200
            empty_resp.json.return_value = {"data": {"after": "t3_empty_next", "children": []}}
            source.http.get = MagicMock(return_value=empty_resp)

            seen_ids = set()
            source._fetch_via_json(target_count=5, seen_ids=seen_ids)

            # After 3 empty pages, it must have broken early and marked source exhausted
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute("SELECT status FROM sources WHERE name='funny' AND listing_mode='top_month'")
            status = cur.fetchone()[0]
            conn.close()
            self.assertEqual(status, "exhausted")



if __name__ == "__main__":
    unittest.main()




class TestRedditSourceProductionStartup(unittest.TestCase):
    """
    Regression test: in production (no pytest env vars) RedditSource.__init__ evaluates
    `"pytest" in sys.modules`. A missing `import sys` crashed every scraper run with NameError,
    but tests never noticed because PYTEST_CURRENT_TEST short-circuited the check.
    """

    def test_init_without_pytest_env_vars_does_not_crash(self):
        import tempfile
        env_keys = ["PYTEST_CURRENT_TEST", "PYTEST_RUNNING", "TESTING"]
        saved = {k: os.environ.pop(k) for k in env_keys if k in os.environ}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = {
                    "db_path": os.path.join(tmp, "videos.db"),
                    "reddit": {"subreddits": ["funny"], "listings": ["hot"], "rate_limit_delay": [0.0, 0.0]},
                }
                with patch.object(RedditSource, "_init_praw"):
                    source = RedditSource(cfg)
                self.assertEqual(source.listings, ["hot"])
        finally:
            os.environ.update(saved)
