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

import logging
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

    A route may also hold a pre-built `_Response`, which is passed through
    rather than re-wrapped -- that is how a test sets a non-200 status or a
    body that will not parse. Wrapping it instead would hand production a
    `_Response` where it expects parsed JSON, which is a fake shaping the
    code rather than standing in for the network.
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
                if isinstance(answer, _Response):
                    return answer
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
        with tmdb_art._cache_lock:
            tmdb_art._unidentified_seen.clear()

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

    def _capture(self, level=logging.WARNING):
        """Collect this module's log records for the rest of the test.

        Shared here rather than on `LoggingTest`, because more than one
        class asserts on log text and the helper is not specific to the
        level-discipline cases it was first written for.
        """
        capture = _LogCapture(level)
        self.addCleanup(capture.__exit__)
        return capture.__enter__()


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

#: An episode as Jellyfin actually builds one: the provider ids are the
#: **episode's own**, not the series'. ``TmdbEpisodeProvider`` and
#: ``TvdbEpisodeProvider`` both write the episode's external ids here, which
#: is why an episode has to climb to its series for a poster.
REAL_EPISODE = {
    "Id": "episode-real",
    "Type": "Episode",
    "Name": "The One With The Art",
    "SeriesName": "Some Show",
    "SeriesId": "series-1",
    "IndexNumber": 3,
    "ParentIndexNumber": 1,
    "ProviderIds": {"Tmdb": "63056", "Tvdb": "3254641",
                    "Imdb": "tt1480055"},
}

#: The series that episode belongs to, as `get_item` returns it.
SERIES = {
    "Id": "series-1",
    "Type": "Series",
    "Name": "Some Show",
    "ProviderIds": {"Tmdb": "1399", "Tvdb": "121361",
                    "Imdb": "tt0944947"},
}


class _Jellyfin:
    """Stands in for `client.jellyfin`, recording what was asked for."""

    def __init__(self, items, error=None):
        self.items = items
        self.error = error
        self.fetched = []

    def get_item(self, item_id, fields=None):
        self.fetched.append((item_id, fields))
        if self.error is not None:
            raise self.error
        if item_id not in self.items:
            raise KeyError(item_id)
        return self.items[item_id]


class _JellyfinClient:
    def __init__(self, jellyfin):
        self.jellyfin = jellyfin

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

    def test_each_type_wins_its_own_namespace_when_both_answer(self):
        """A film is a film and a show is a show, from one shared id shape.

        ``/find`` answers an IMDb id in both ``movie_results`` and
        ``tv_results`` when the title exists in both namespaces, which is
        often. A fixed list order would therefore hand one of these the
        other's poster -- the failure being silent, because a poster is a
        poster until you look at whose it is.
        """
        answers = {
            "find/tt1": {"movie_results": [{"id": 603}],
                         "tv_results": [{"id": 1399}]},
            "movie/603": POSTER,
            "tv/1399": POSTER,
        }
        for item, expected in (
                (dict(MOVIE, ProviderIds={"Imdb": "tt1"}), "movie/603"),
                (dict(EPISODE, ProviderIds={"Imdb": "tt1"}), "tv/1399")):
            fake = _FakeTMDB(dict(answers))
            self._patch(fake)
            self.settled(item)
            self.assertIn(expected, fake.paths(),
                          "%s did not resolve in its own namespace: %s"
                          % (item["Type"], fake.paths()))

    def test_a_tmdb_id_beats_an_imdb_id(self):
        item = dict(MOVIE, ProviderIds={"Tmdb": "603", "Imdb": "tt0133093"})
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        self.settled(item)
        self.assertEqual(fake.paths(), ["movie/603"],
                         "an exact id does not need a search")

    def test_a_tmdb_id_beats_a_tvdb_id_on_a_show(self):
        # An exact TMDB id needs no /find at all, whatever else is on the
        # item -- TVDB included.
        item = dict(EPISODE, ProviderIds={"Tmdb": "1399", "Tvdb": "121361"})
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        self.settled(item)
        self.assertEqual(fake.paths(), ["tv/1399"])


class TvdbTest(_TMDBTestCase):
    """TVDB first for telly: the id Jellyfin's own episode metadata came from.

    The rule is TV-only and TVDB-before-IMDb. Both halves are asserted here,
    because each is wrong in a way that still "works": trying TVDB for a film
    costs a round trip that can never succeed, and preferring IMDb for a show
    reintroduces exactly the disagreement this ordering exists to avoid.
    """

    def test_a_show_with_a_tvdb_id_asks_for_it_by_tvdb_id(self):
        fake = _FakeTMDB({
            "find/121361": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        item = dict(EPISODE, ProviderIds={"Tvdb": "121361"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/121361", "tv/1399"])

    def test_tvdb_is_preferred_over_imdb_for_a_show(self):
        # The ordering itself, which is what the feature is. Both ids are
        # present and only the TVDB one may be asked about.
        fake = _FakeTMDB({
            "find/121361": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        item = dict(EPISODE, ProviderIds={"Tvdb": "121361",
                                          "Imdb": "tt0944947"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/121361", "tv/1399"])
        self.assertNotIn("find/tt0944947", fake.paths(),
                         "IMDb was asked about with a TVDB id available")

    def test_the_tvdb_request_declares_the_tvdb_source(self):
        # The parameter is the whole mechanism: the same numeric string sent
        # as imdb_id is a miss, and a miss caches as "no art" for six hours.
        seen = {}

        def capture(params):
            seen.update(params or {})
            return {"tv_results": [{"id": 1399}]}

        fake = _FakeTMDB({"find/121361": capture, "tv/1399": POSTER})
        self._patch(fake)
        self.settled(dict(EPISODE, ProviderIds={"Tvdb": "121361"}))
        self.assertEqual(seen.get("external_source"), "tvdb_id")

    def test_a_series_uses_tvdb_too(self):
        fake = _FakeTMDB({
            "find/121361": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        item = {"Id": "s1", "Type": "Series", "Name": "Some Show",
                "ProviderIds": {"Tvdb": "121361"}}
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/121361", "tv/1399"])

    def test_a_season_uses_tvdb_too(self):
        fake = _FakeTMDB({
            "find/121361": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        item = {"Id": "se1", "Type": "Season", "Name": "Season 1",
                "ProviderIds": {"Tvdb": "121361"}}
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/121361", "tv/1399"])

    def test_a_movie_never_asks_by_tvdb_id(self):
        # /find has no movie namespace for tvdb_id, so asking would be a
        # guaranteed miss -- and the miss is cached, so it would also stop
        # the IMDb id that *can* answer from ever being tried.
        fake = _FakeTMDB({"find/tt0133093": {"movie_results": [{"id": 603}]},
                          "movie/603": POSTER})
        self._patch(fake)
        item = dict(MOVIE, ProviderIds={"Tvdb": "999", "Imdb": "tt0133093"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/tt0133093", "movie/603"])
        self.assertNotIn("find/999", fake.paths())

    def test_a_movie_with_only_a_tvdb_id_is_unidentified(self):
        item = dict(MOVIE, ProviderIds={"Tvdb": "999"})
        fake = _FakeTMDB({})
        self._patch(fake)
        self.assertEqual(tmdb_art.lookup(item), (None, None))
        self._join_workers()
        self.assertEqual(fake.calls, [])

    def test_a_season_result_list_is_not_mistaken_for_a_poster(self):
        """The trap in the TVDB result shape.

        A TVDB id can answer in ``tv_season_results``, whose entries are
        seasons -- they have an ``id`` and no ``poster_path``. Taking the
        first list that answers would read a season's id as the series' and
        either 404 on the poster or, worse, fetch a season's key art. Only
        ``tv_results`` is read, and an answer in the season list is a miss.
        """
        fake = _FakeTMDB({"find/121361": {
            "tv_results": [],
            "tv_season_results": [{"id": 3624, "name": "Season 1"}],
            "tv_episode_results": [{"id": 63056}],
            "movie_results": [],
        }})
        self._patch(fake)
        item = dict(EPISODE, ProviderIds={"Tvdb": "121361"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/121361"],
                         "a season's id was used to fetch a poster")

    def test_the_series_list_is_preferred_when_both_answer(self):
        fake = _FakeTMDB({"find/121361": {
            "tv_results": [{"id": 1399}],
            "tv_season_results": [{"id": 3624}],
        }, "tv/1399": POSTER})
        self._patch(fake)
        item = dict(EPISODE, ProviderIds={"Tvdb": "121361"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/121361", "tv/1399"])

    def test_imdb_is_still_the_fallback_when_there_is_no_tvdb_id(self):
        # The half that must not have been broken by adding TVDB.
        fake = _FakeTMDB({
            "find/tt0944947": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        item = dict(EPISODE, ProviderIds={"Imdb": "tt0944947"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/tt0944947", "tv/1399"])

    def test_an_empty_tvdb_value_falls_through_to_imdb(self):
        # Jellyfin writes empty strings for unmatched providers.
        fake = _FakeTMDB({
            "find/tt0944947": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        item = dict(EPISODE, ProviderIds={"Tvdb": "  ",
                                          "Imdb": "tt0944947"})
        self.settled(item)
        self.assertEqual(fake.paths(), ["find/tt0944947", "tv/1399"])

    def test_tvdb_and_imdb_ids_for_one_show_do_not_share_a_cache_entry(self):
        # Same numbered id arriving under two sources is two answers, not
        # one: a key without the provider would collide them.
        self.assertNotEqual(
            tmdb_art._cache_key(dict(EPISODE, ProviderIds={"Tvdb": "1399"})),
            tmdb_art._cache_key(dict(EPISODE, ProviderIds={"Imdb": "1399"})))


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


class _LogCapture:
    """Record what the module logs, at the levels a real log would keep.

    The level matters as much as the text: the default ``mpv_log_level`` is
    ``info``, so a line that only fires at debug reaches nobody's ``log.txt``
    -- which is the defect these tests exist to catch.

    Attached to the ``tmdb_art`` logger itself and **idempotent**, because a
    capture that leaks its handler makes the next test's log line appear
    twice, and a count assertion then fails for a reason that has nothing to
    do with the code under test. `_detach_all` is the belt to that braces:
    it clears anything a previous, failed test left behind.
    """

    def __init__(self, level=logging.WARNING):
        self.records = []
        self.level = level
        self._handler = None
        self._saved_level = logging.getLogger("tmdb_art").level
        self._logger = logging.getLogger("tmdb_art")

    def _attach(self):
        if self._handler is not None:
            return self
        self._detach_all()
        self._handler = logging.Handler()
        self._handler._jms_test_capture = True
        self._handler.emit = self.records.append
        self._saved_level = self._logger.level
        self._logger.setLevel(self.level)
        self._logger.addHandler(self._handler)
        return self

    def _detach_all(self):
        """Remove every handler this test class could have left attached."""
        for handler in list(self._logger.handlers):
            if getattr(handler, "_jms_test_capture", False):
                self._logger.removeHandler(handler)
        self._logger.handlers = [
            h for h in self._logger.handlers
            if not getattr(h, "_jms_test_capture", False)]

    def __enter__(self):
        return self._attach()

    def __exit__(self, *exc):
        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler = None
        self._logger.setLevel(self._saved_level)
        return False

    def messages(self, level=None):
        return [r.getMessage() for r in self.records
                if level is None or r.levelno == level]

    def text(self):
        return "\n".join(self.messages())


class LoggingTest(_TMDBTestCase):
    """The feature is diagnosable from `log.txt`, which is the whole point.

    Each case here pins one cause *and* the level it is reported at. The
    level is the part that is easy to get wrong and invisible when wrong: a
    `debug` line for a broken configuration is indistinguishable, to the
    person reading the log, from no line at all.
    """

    def test_a_missing_key_is_a_warning_that_names_the_setting(self):
        settings.discord_tmdb_api_key = ""
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
        self.assertIn("discord_tmdb_api_key", logs.text())
        self.assertTrue(
            any(r.levelno == logging.WARNING
                for r in logs.records),
            "a configuration that cannot work was not a warning")

    def test_the_missing_key_warning_is_not_repeated_every_tick(self):
        # This is called every few seconds for the whole of a film. A warning
        # per tick is how people learn to ignore warnings.
        settings.discord_tmdb_api_key = ""
        with self._capture() as logs:
            for _ in range(20):
                tmdb_art.lookup(MOVIE)
        self.assertEqual(len(logs.records), 1,
                         "the no-key warning repeated on every tick")

    def test_the_missing_key_warning_returns_after_the_feature_is_toggled(self):
        # Someone who turns it off, pastes a key and turns it back on should
        # be told again if they got that wrong, not silenced forever.
        settings.discord_tmdb_api_key = ""
        tmdb_art.lookup(MOVIE)
        settings.discord_tmdb_enabled = False
        tmdb_art.lookup(MOVIE)
        settings.discord_tmdb_enabled = True
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
        self.assertEqual(len(logs.records), 1)

    def test_the_feature_being_off_is_not_a_warning(self):
        # Off is the default of every install that never asked for this; a
        # warning per tick would be noise that trains people to ignore them.
        settings.discord_tmdb_enabled = False
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
        self.assertEqual(logs.records, [])

    def test_no_provider_id_says_so_and_names_the_item(self):
        item = {"Id": "x", "Type": "Movie", "Name": "A Home Video"}
        with self._capture(logging.INFO) as logs:
            tmdb_art.lookup(item)
            tmdb_art.lookup(item)
        self.assertIn("A Home Video", logs.text())
        self.assertEqual(len(logs.records), 1,
                         "an unidentified item logged once per tick")

    def test_a_bad_key_is_a_warning_naming_the_status(self):
        self._patch(_FakeTMDB({"movie/603": _Response(
            {"status_message": "Invalid API key"}, status_code=401)}))
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertIn("401", logs.text())
        self.assertIn("Invalid API key", logs.text())

    def test_a_connection_failure_names_the_exception_type(self):
        # "no route to host" and "connection refused" are different user
        # problems, and only the exception type tells them apart.
        self._patch(_FakeTMDB({"movie/603": OSError("no route to host")}))
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertIn("OSError", logs.text())
        self.assertIn("no route to host", logs.text())

    def test_unparseable_json_is_a_warning(self):
        class _NotJSON(_Response):
            def json(self):
                raise ValueError("Expecting value: line 1 column 1")

        self._patch(_FakeTMDB({"movie/603": _NotJSON(None)}))
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertIn("ValueError", logs.text())

    def test_a_title_with_no_poster_says_so_rather_than_staying_silent(self):
        # 200 and a known title: the answer that looks identical to "the
        # lookup never ran" unless it is written down.
        self._patch(_FakeTMDB({"movie/603": {"poster_path": None}}))
        with self._capture(logging.INFO) as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertIn("No TMDB cover art", logs.text())
        self.assertIn("A Film", logs.text())

    def test_a_find_with_no_match_reports_the_single_no_art_line(self):
        # The per-source /find trace was removed; what remains is one line
        # saying the lookup produced nothing.
        item = dict(MOVIE, ProviderIds={"Imdb": "tt0000000"})
        self._patch(_FakeTMDB(
            {"find/tt0000000": {"movie_results": [], "tv_results": []}}))
        with self._capture(logging.INFO) as logs:
            tmdb_art.lookup(item)
            self._join_workers()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("No TMDB cover art", logs.text())

    def test_a_tvdb_miss_reports_the_same_one_line(self):
        item = dict(EPISODE, ProviderIds={"Tvdb": "121361"})
        self._patch(_FakeTMDB(
            {"find/121361": {"tv_results": [], "tv_season_results": []}}))
        with self._capture(logging.INFO) as logs:
            tmdb_art.lookup(item)
            self._join_workers()
        self.assertEqual(len(logs.records), 1)
        self.assertIn("No TMDB cover art", logs.text())

    def test_a_wrong_typed_poster_path_is_a_warning(self):
        # The silent-bad-URL case: art missing, with a perfectly good 200.
        self._patch(_FakeTMDB({"movie/603": {"poster_path": {"oops": 1}}}))
        with self._capture() as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertIn("dict", logs.text())

    def test_the_success_path_is_silent(self):
        # A working feature is not news, and a line per film would be noise
        # in every log that has this switched on.
        self._patch(_FakeTMDB({"movie/603": POSTER}))
        with self._capture(logging.DEBUG) as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertEqual(logs.records, [],
                         "the success path logged: %r" % (logs.text(),))

    def test_the_key_never_reaches_any_log_line(self):
        # It is a query parameter, so a line logging the whole URL would
        # leak it into log.txt and every bug report attached to an issue.
        # Driven at DEBUG so *any* future line is caught, not just the ones
        # that exist today.
        self._patch(_FakeTMDB(
            {"movie/603": _Response({"status_message": "nope"},
                                    status_code=401)}))
        with self._capture(logging.DEBUG) as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertTrue(logs.records, "nothing was logged to check")
        self.assertNotIn("test-key", logs.text())
        self.assertNotIn("api_key", logs.text())

    def test_every_failure_line_reaches_an_info_level_log(self):
        # The regression this class exists for: failure paths were `debug`,
        # and the default `mpv_log_level` is `info`, so a user with the
        # feature on and broken saw nothing at all.
        cases = {
            "no key": (lambda: setattr(settings,
                                       "discord_tmdb_api_key", ""), MOVIE),
            "unidentified": (lambda: None,
                             {"Id": "x", "Type": "Movie", "Name": "N"}),
        }
        for name, (setup, item) in cases.items():
            setup()
            with self._capture(logging.INFO) as logs:
                tmdb_art.lookup(item)
            self.assertTrue(logs.records,
                            "%s produced no line at info level" % name)
            settings.discord_tmdb_api_key = "test-key"

    def test_a_rejected_request_reaches_an_info_level_log(self):
        self._patch(_FakeTMDB({"movie/603": _Response({}, status_code=429)}))
        with self._capture(logging.INFO) as logs:
            tmdb_art.lookup(MOVIE)
            self._join_workers()
        self.assertIn("429", logs.text())


class EpisodeSeriesTest(_TMDBTestCase):
    """An episode is looked up as its *series*, never as itself.

    This is the case a fixture gets wrong by accident: ``EPISODE`` above
    carries the series' TMDB id, so it passes whether or not the climb
    happens. ``REAL_EPISODE`` carries what Jellyfin actually puts there --
    the episode's own ids -- and that is what these use.

    The property: whatever the episode's own ids are, the request TMDB
    receives must be about the series. Both halves matter. The episode's
    ``Tmdb`` id passed straight to ``/tv/{id}`` is the loud failure (404, or
    the wrong show); the subtle one is an episode that *does* resolve, in
    ``tv_episode_results``, and hands Discord a 16:9 screenshot where the
    square cover art goes.
    """

    def _client(self, items=None, error=None):
        return _JellyfinClient(_Jellyfin(items or {"series-1": SERIES},
                                         error=error))

    def _find_payload(self):
        return {"tv_results": [{"id": 1399, "name": "Some Show",
                                "poster_path": "/series.jpg"}],
                "tv_episode_results": [{"id": 63056, "still_path": "/still.jpg"}],
                "movie_results": [], "person_results": [],
                "tv_season_results": []}

    def test_the_episodes_own_tmdb_id_is_not_used(self):
        # The loud failure: /tv/63056 is the episode, not the show.
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        tmdb_art.lookup(REAL_EPISODE, self._client())
        self._join_workers()
        self.assertEqual(fake.paths(), ["tv/1399"],
                         "the episode's own TMDB id was used as a series id")

    def test_the_series_provider_ids_drive_the_lookup(self):
        # The series has a Tmdb id, so no /find is needed at all.
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        url, label = tmdb_art.lookup(REAL_EPISODE, self._client())
        if url is None:
            self._join_workers()
            url, label = tmdb_art.lookup(REAL_EPISODE, self._client())
        self.assertEqual(url, "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(label, "Some Show")
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_it_climbs_through_find_when_the_series_has_only_a_tvdb_id(self):
        series = dict(SERIES, ProviderIds={"Tvdb": "121361"})
        fake = _FakeTMDB({
            "find/121361": {"tv_results": [{"id": 1399}]},
            "tv/1399": POSTER,
        })
        self._patch(fake)
        topic = tmdb_art.lookup(REAL_EPISODE, self._client({"series-1": series}))
        self._join_workers()
        self.assertEqual(fake.paths(), ["find/121361", "tv/1399"])

    def test_an_episode_id_answering_in_the_episode_list_is_not_used(self):
        # The subtle failure: the episode's TVDB id resolving to the episode,
        # whose art is `still_path` -- a screenshot.
        fake = _FakeTMDB({"find/3254641": self._find_payload(),
                          "tv/1399": POSTER})
        self._patch(fake)
        series_no_tmdb = dict(SERIES, ProviderIds={"Tvdb": "121361"})
        tmdb_art.lookup(REAL_EPISODE, self._client({"series-1": series_no_tmdb}))
        self._join_workers()
        self.assertNotIn("find/3254641", fake.paths(),
                         "the episode's own id was looked up")
        self.assertNotIn("tv_episode_results", " ".join(fake.paths()))

    def test_the_series_is_fetched_once_per_series_not_per_tick(self):
        # This runs every few seconds for a whole episode; a get_item per
        # tick is a request per tick against the user's own server.
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        jellyfin = _Jellyfin({"series-1": SERIES})
        client = _JellyfinClient(jellyfin)
        for _ in range(6):
            tmdb_art.lookup(REAL_EPISODE, client)
            self._join_workers()
        self.assertEqual(len(jellyfin.fetched), 1,
                         "the series was re-fetched on every tick")

    def test_the_series_request_asks_for_provider_ids(self):
        # Without the field the response has no ProviderIds and the climb
        # silently resolves nothing.
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        jellyfin = _Jellyfin({"series-1": SERIES})
        tmdb_art.lookup(REAL_EPISODE, _JellyfinClient(jellyfin))
        self._join_workers()
        self.assertEqual([f for _, f in jellyfin.fetched],
                         [tmdb_art.SERIES_FIELDS])
        self.assertIn("ProviderIds", tmdb_art.SERIES_FIELDS)

    def test_a_series_that_cannot_be_fetched_falls_back_to_the_episode(self):
        # The previous behaviour, which is still right for a server that has
        # a series-level id on the episode for whatever reason.
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        client = self._client(error=RuntimeError("server went away"))
        tmdb_art.lookup(EPISODE, client)
        self._join_workers()
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_a_series_without_provider_ids_falls_back_to_the_episode(self):
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        bare = dict(SERIES, ProviderIds={})
        tmdb_art.lookup(EPISODE, self._client({"series-1": bare}))
        self._join_workers()
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_no_client_falls_back_to_the_episode(self):
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        tmdb_art.lookup(EPISODE, None)
        self._join_workers()
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_an_episode_with_no_series_id_falls_back_to_itself(self):
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        item = dict(EPISODE, SeriesId=None)
        tmdb_art.lookup(item, self._client())
        self._join_workers()
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_a_movie_never_climbs(self):
        # Only episodes do; a film has no series and the client must not be
        # consulted at all.
        fake = _FakeTMDB({"movie/603": POSTER})
        self._patch(fake)
        jellyfin = _Jellyfin({})
        tmdb_art.lookup(MOVIE, _JellyfinClient(jellyfin))
        self._join_workers()
        self.assertEqual(jellyfin.fetched, [])

    def test_a_series_is_not_climbed_from(self):
        fake = _FakeTMDB({"tv/1399": POSTER})
        self._patch(fake)
        jellyfin = _Jellyfin({})
        tmdb_art.lookup(SERIES, _JellyfinClient(jellyfin))
        self._join_workers()
        self.assertEqual(jellyfin.fetched, [])
        self.assertEqual(fake.paths(), ["tv/1399"])

    def test_a_raising_client_is_not_an_exception(self):
        # client.jellyfin could be anything; the lookup must survive it.
        class Boom:
            @property
            def jellyfin(self):
                raise RuntimeError("no attribute")

        fake = _FakeTMDB({"tv/1399": POSTER, "find/3254641": {}})
        self._patch(fake)
        self.assertEqual(
            tmdb_art.lookup(dict(REAL_EPISODE), Boom()), (None, None))
        self._join_workers()

    def test_the_log_describes_the_series_not_the_episode(self):
        # "No id on this item" is misleading when the item reported is the
        # episode and the series is what was actually asked about.
        bare = dict(SERIES, ProviderIds={})
        self._patch(_FakeTMDB({}))
        with self._capture(logging.INFO) as logs:
            tmdb_art.lookup(dict(REAL_EPISODE), self._client({"series-1": bare}))
            self._join_workers()
        self.assertIn("Some Show", logs.text())
        self.assertIn("Series", logs.text())


if __name__ == "__main__":
    unittest.main()
