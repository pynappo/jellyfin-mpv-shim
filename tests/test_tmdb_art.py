"""Cover art looked up in TMDB, for Discord Rich Presence.

Two properties are load-bearing.

**Nothing here raises, ever.** The lookup runs from the progress timer inside
the presence block, where an exception costs the whole presence update -- so
every failure has to be a ``(None, None)`` and the caller has to be free to
fall through to the Jellyfin URL. Several tests below feed it a broken world
for exactly this reason.

**The id has to come from the item, and the namespace has to match its type.**
A poster is keyed by (movie|tv, id); handing TMDB a film id in the tv
namespace is a 404, and reading the wrong ``/find`` result list is how an
episode ends up wearing a film's poster. Those are the two ways this feature
fails *quietly* -- the image is simply wrong, or simply absent -- so they are
pinned here.

No network: `requests.get` is replaced, but everything after it is the real
code path, per the discipline in `tests/test_mpvtk_cast.py`.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest

from jellyfin_mpv_shim import tmdb_art
from jellyfin_mpv_shim.conf import settings


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeTMDB:
    """Stands in for `requests.get`, routed by URL.

    `routes` maps a path fragment to a payload (or to an exception to raise).
    A request whose fragment matches nothing is recorded as a miss, which is
    how the tests below assert that a call was *not* made.
    """

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append(url)
        for fragment, answer in self.routes.items():
            if fragment in url:
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    answer = answer(params or {})
                return _Response(answer)
        raise AssertionError("unexpected TMDB request: %s" % url)

    def paths(self):
        return [url.split("/3/", 1)[-1].split("?")[0] for url in self.calls]


class _TMDBTestCase(unittest.TestCase):
    def setUp(self):
        tmdb_art.clear_cache()
        self._saved = (settings.discord_tmdb_enabled,
                       settings.discord_tmdb_api_key,
                       settings.discord_tmdb_language)
        settings.discord_tmdb_enabled = True
        settings.discord_tmdb_api_key = "test-key"
        settings.discord_tmdb_language = ""
        self.addCleanup(self._restore)

    def _restore(self):
        (settings.discord_tmdb_enabled,
         settings.discord_tmdb_api_key,
         settings.discord_tmdb_language) = self._saved
        self._join_workers()
        tmdb_art.clear_cache()

    def _join_workers(self):
        """Wait for every worker thread this test started.

        `lookup` answers from the cache and starts a worker on a miss, so a
        test that patched `requests.get` must not leave that worker running
        into the next test with the patch already uninstalled.
        """
        import threading
        for thread in list(threading.enumerate()):
            if thread.name == "tmdb-art" and thread.is_alive():
                thread.join(timeout=10)

    def settled(self, item):
        """Run `lookup` to completion and return its answer.

        The first call cannot return art -- that is the point of the design --
        so a test that wants the answer asks, waits, and asks again.
        """
        self.assertEqual(tmdb_art.lookup(item), (None, None),
                         "the first call blocked or answered from nothing")
        self._join_workers()
        return tmdb_art.lookup(item)

    def _patch(self, fake):
        import requests
        real = requests.get
        self.addCleanup(lambda: setattr(requests, "get", real))
        requests.get = fake


MOVIE = {
    "Id": "movie-1",
    "Type": "Movie",
    "Name": "A Film",
    "ProviderIds": {"Tmdb": "603"},
}

EPISODE = {
    "Id": "episode-1",
    "Type": "Episode",
    "Name": "The One With The Art",
    "SeriesName": "Some Show",
    "SeriesId": "series-1",
    "ProviderIds": {"Tmdb": "1399"},
}

POSTER = {"poster_path": "/poster.jpg"}


class IdSelectionTest(_TMDBTestCase):
    """Which id is asked about, and in which namespace."""

    def test_a_movie_asks_the_movie_namespace(self):
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        url, label = self.settled(MOVIE)
        self.assertEqual(fake.paths(), ["movie/603"])
        self.assertEqual(url, "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(label, "A Film")

    def test_an_episode_asks_the_tv_namespace_for_the_series(self):
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        url, label = self.settled(EPISODE)
        self.assertEqual(fake.paths(), ["tv/1399"])
        self.assertEqual(label, "Some Show",
                         "the label is the series, matching the poster")

    def test_a_series_asks_the_tv_namespace(self):
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        self.settled(dict(EPISODE, Type="Series"))
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_the_lowercase_provider_key_is_read_too(self):
        # Jellyfin has written both spellings across versions.
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(dict(MOVIE, ProviderIds={"TMDB": "603"}))
        self.assertEqual(fake.paths(), ["movie/603"])

    def test_an_episode_imdb_id_reads_the_tv_result_list(self):
        # The whole point: the episode's own id lives in the tv namespace,
        # and reading `movie_results` would paint a film's poster on it.
        item = dict(EPISODE, ProviderIds={"Imdb": "tt0944947"})
        fake = _FakeTMDB({
            "find/tt0944947": {
                "movie_results": [{"id": 999}],
                "tv_results": [{"id": 1399}],
            },
            "tv/1399": POSTER,
        })
        self._patch(fake)
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/tt0944947", "tv/1399"])

    def test_a_movie_imdb_id_reads_the_movie_result_list(self):
        item = dict(MOVIE, ProviderIds={"Imdb": "tt0133093"})
        fake = _FakeTMDB({
            "find/tt0133093": {
                "movie_results": [{"id": 603}],
                "tv_results": [{"id": 1399}],
            },
            "movie/603": POSTER,
        })
        self._patch(fake)
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/tt0133093", "movie/603"])

    def test_a_tmdb_id_beats_an_imdb_id(self):
        item = dict(MOVIE, ProviderIds={"Tmdb": "603", "Imdb": "tt0133093"})
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(item)
        self.assertEqual(fake.paths(), ["movie/603"],
                         "an exact id does not need a search")


class OptInTest(_TMDBTestCase):
    """Two switches, both required, and neither may be assumed."""

    def test_disabled_makes_no_request(self):
        settings.discord_tmdb_enabled = False
        fake = _FakeTMDB({})
        self._patch(fake)
        self.assertEqual(tmdb_art.lookup(MOVIE), (None, None))
        self.assertEqual(fake.calls, [])

    def test_no_key_makes_no_request(self):
        settings.discord_tmdb_api_key = ""
        fake = _FakeTMDB({})
        self._patch(fake)
        self.assertEqual(tmdb_art.lookup(MOVIE), (None, None))
        self.assertEqual(fake.calls, [])

    def test_a_whitespace_key_is_no_key(self):
        settings.discord_tmdb_api_key = "   "
        fake = _FakeTMDB({})
        self._patch(fake)
        self.assertEqual(tmdb_art.lookup(MOVIE), (None, None))
        self.assertEqual(fake.calls, [])

    def test_the_key_is_sent_and_the_language_is_optional(self):
        settings.discord_tmdb_language = "de-DE"
        seen = {}

        def capture(params):
            seen.update(params)
            return POSTER

        fake = _FakeTMDB({"movie/603": capture})
        self._patch(fake)
        self.settled(MOVIE)
        self.assertEqual(seen.get("api_key"), "test-key")
        self.assertEqual(seen.get("language"), "de-DE")

    def test_no_language_setting_sends_no_language(self):
        seen = {}

        def capture(params):
            seen.update(params)
            return POSTER

        fake = _FakeTMDB({"movie/603": capture})
        self._patch(fake)
        self.settled(MOVIE)
        self.assertNotIn("language", seen,
                         "an empty setting is not a language code")


class FailureTest(_TMDBTestCase):
    """Every failure is a silent (None, None): this runs on the timer."""

    def _expect_silent(self, routes, item=MOVIE):
        fake = _FakeTMDB(routes)
        self._patch(fake)

        def call():
            self.assertEqual(tmdb_art.lookup(item), (None, None))
            self._join_workers()
            self.assertEqual(tmdb_art.lookup(item), (None, None))

        call()
        return fake

    def test_a_network_error_is_silent(self):
        self._expect_silent({"movie/603": OSError("no route to host")})

    def test_a_timeout_is_silent(self):
        self._expect_silent({"movie/603": TimeoutError("timed out")})

    def test_a_non_200_is_silent(self):
        self._expect_silent(
            {"movie/603": _Response({}, status_code=401)})

    def test_a_rate_limit_is_silent(self):
        self._expect_silent({"movie/603": _Response({}, status_code=429)})

    def test_an_item_with_no_provider_ids_is_silent(self):
        fake = self._expect_silent({}, item=dict(MOVIE, ProviderIds={}))
        self.assertEqual(fake.calls, [])

    def test_an_item_with_no_provider_ids_key_at_all_is_silent(self):
        item = {"Id": "x", "Type": "Movie", "Name": "Bare"}
        fake = self._expect_silent({}, item=item)
        self.assertEqual(fake.calls, [])

    def test_none_is_not_an_exception(self):
        self.assertEqual(tmdb_art.lookup(None), (None, None))

    def test_a_find_with_no_matching_result_is_silent(self):
        item = dict(MOVIE, ProviderIds={"Imdb": "tt0000000"})
        self._expect_silent(
            {"find/tt0000000": {"movie_results": [], "tv_results": []}},
            item=item)

    def test_no_poster_path_is_silent(self):
        self._expect_silent({"movie/603": {"poster_path": None}})

    def test_a_response_that_is_not_a_dict_is_silent(self):
        # `json()` on a proxy's HTML error page, or a truncated body.
        self._expect_silent({"movie/603": ["not", "a", "dict"]})

    def test_an_unexpected_shape_does_not_escape(self):
        self._expect_silent({"movie/603": {"poster_path": {"oops": 1}}})


class CacheTest(_TMDBTestCase):
    """The timer calls this every few seconds for the whole of a film."""

    def test_a_second_lookup_makes_no_second_request(self):
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(MOVIE)
        for _ in range(5):
            url, _ = tmdb_art.lookup(MOVIE)
            self.assertIsNotNone(url)
        self._join_workers()
        self.assertEqual(len(fake.calls), 1,
                         "the progress timer was allowed to spam TMDB")

    def test_a_miss_starts_exactly_one_worker(self):
        # Ten timer ticks between the request going out and its answer
        # arriving must not be ten requests.
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        for _ in range(10):
            tmdb_art.lookup(MOVIE)
        self._join_workers()
        self.assertEqual(len(fake.calls), 1)

    def test_the_cached_label_is_returned_every_time(self):
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(MOVIE)
        labels = {tmdb_art.lookup(MOVIE)[1] for _ in range(3)}
        self.assertEqual(labels, {"A Film"})

    def test_a_worker_finishing_never_leaves_a_gap_for_a_duplicate(self):
        """The in-flight guard, driven through the threads.

        Six ticks arrive while the one request is open. Only the in-flight
        marker is holding them off -- there is no cache entry yet to answer
        from -- so without it every tick starts its own request, which is
        thousands per film across a two-hour runtime.

        The publish and the marker-drop happen in one critical section
        (`_cache_put`), so the two threads cannot meet in the gap between
        them; this test is what fails when that guard is removed.
        """
        import threading
        started = threading.Event()
        release = threading.Event()
        calls = []

        def slow(url, params=None, timeout=None):
            calls.append(url)
            started.set()
            release.wait(timeout=5)
            return _Response(POSTER)

        self._patch(slow)
        tmdb_art.lookup(MOVIE)
        self.assertTrue(started.wait(timeout=5), "the worker never ran")

        # The timer now ticks five times *while* the answer is being built.
        # Only the in-flight marker is holding these off.
        for _ in range(5):
            self.assertEqual(tmdb_art.lookup(MOVIE), (None, None))
        release.set()
        self._join_workers()
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(tmdb_art.lookup(MOVIE)[0])

    def test_a_show_with_no_poster_is_not_asked_about_again(self):
        # The miss is as expensive as the hit and happens just as often.
        fake = _FakeTMDB({"movie/603": {"poster_path": None}})
        self._patch(fake)
        for _ in range(4):
            tmdb_art.lookup(MOVIE)
        self._join_workers()
        for _ in range(4):
            self.assertEqual(tmdb_art.lookup(MOVIE), (None, None))
        self.assertEqual(len(fake.calls), 1)

    def test_two_items_are_cached_separately(self):
        fake = _FakeTMDB({"movie/603": POSTER, "tv/1399": POSTER})
        self._patch(fake)
        self.settled(MOVIE)
        self.settled(EPISODE)
        tmdb_art.lookup(MOVIE)
        self._join_workers()
        self.assertEqual(sorted(fake.paths()), ["movie/603", "tv/1399"])

    def test_clearing_the_cache_asks_again(self):
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(MOVIE)
        tmdb_art.clear_cache()
        self.settled(MOVIE)
        self.assertEqual(len(fake.calls), 2)

    def test_the_cache_lives_on_the_module_not_the_item(self):
        # Two `Media` objects for the same film -- a re-play, or a queue item
        # and its next-up copy -- are the same lookup.
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(dict(MOVIE))
        self.assertEqual(tmdb_art.lookup(dict(MOVIE)),
                         ("https://image.tmdb.org/t/p/w500/poster.jpg",
                          "A Film"))
        self.assertEqual(len(fake.calls), 1)

    def test_the_cache_stays_bounded(self):
        # A queue of a few hundred items must not grow it without limit.
        fake = _FakeTMDB({"movie/": POSTER})
        self._patch(fake)
        for index in range(tmdb_art._CACHE_MAX + 40):
            tmdb_art.lookup(dict(MOVIE, ProviderIds={"Tmdb": str(index)}))
        self._join_workers()
        with tmdb_art._cache_lock:
            size = len(tmdb_art._cache)
        self.assertLessEqual(size, tmdb_art._CACHE_MAX)


class BlockingTest(_TMDBTestCase):
    """`lookup` is called on the stop path and must not do network I/O."""

    def test_a_cache_miss_does_not_block(self):
        import threading
        slow = threading.Event()

        def hang(url, params=None, timeout=None):
            slow.wait(timeout=5)
            return _Response(POSTER)

        self._patch(hang)
        # If this waited on the request it would hang for the full five
        # seconds and fail the test's own runtime expectation instead.
        self.assertEqual(tmdb_art.lookup(MOVIE), (None, None))
        slow.set()
        self._join_workers()
        url, label = tmdb_art.lookup(MOVIE)
        self.assertEqual(url,
                         "https://image.tmdb.org/t/p/w500/poster.jpg")

    def test_art_appears_on_a_later_call(self):
        # The property the whole design is for: the first tick draws the
        # Jellyfin logo, and a later tick draws the TMDB poster.
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        first = tmdb_art.lookup(MOVIE)
        self._join_workers()
        later = tmdb_art.lookup(MOVIE)
        self.assertEqual(first, (None, None))
        self.assertIsNotNone(later[0])


if __name__ == "__main__":
    unittest.main()
