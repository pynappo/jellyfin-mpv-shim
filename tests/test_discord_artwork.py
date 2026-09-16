"""The artwork handed to Discord Rich Presence.

One property is load-bearing and the rest are its consequences: **the URL
Discord is given must carry no access token.** Discord fetches
``large_image`` from its own servers, so a URL built the way every other
artwork URL in this client is built would hand the user's Jellyfin token to
a third party -- the same leak ``docs/auth-headers.md`` exists to prevent
for mpv. The fallback to the Jellyfin logo when no safe URL can be built is
therefore the feature, not the absence of one.

The second is that a LAN address is refused, because Discord cannot reach
it -- and that ``discord_public_url`` is the way out of that refusal, which
is what this setting exists for. Jellyfin cannot answer the question itself:
``PublicSystemInfo.LocalAddress`` is derived from the request, so dialling
in on the LAN address returns the LAN address.

The third is that a refusal is a silent ``(None, None)``: every cause has the
same visible symptom -- the logo stays -- and this runs on the progress timer,
so a line per item per few seconds would be noise rather than a way to tell
one cause from another.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import threading
import unittest
import urllib.parse

from jellyfin_mpv_shim import tmdb_art
from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.player_reporting import _discord_art_url


class _Config:
    def __init__(self, server):
        self.data = {"auth.server": server}


class _Client:
    def __init__(self, server="https://jelly.example"):
        self.config = _Config(server)


class _Video:
    def __init__(self, item, client=None):
        self.item = item
        self.client = client if client is not None else _Client()


EPISODE = {
    "Id": "episode-1",
    "Type": "Episode",
    "Name": "The One With The Art",
    "SeriesName": "Some Show",
    "SeriesId": "series-1",
    "SeriesPrimaryImageTag": "tag-series",
    "ImageTags": {"Primary": "tag-episode"},
}

MOVIE = {
    "Id": "movie-1",
    "Type": "Movie",
    "Name": "A Film",
    "ImageTags": {"Primary": "tag-movie"},
}


class _ArtCase(unittest.TestCase):
    """Resets the public-url setting around each case."""

    def setUp(self):
        self._public = settings.discord_public_url
        settings.discord_public_url = ""
        self.addCleanup(self._restore)

    def _restore(self):
        settings.discord_public_url = self._public


class TokenTest(_ArtCase):
    """The whole reason this function exists."""

    def test_an_episode_is_asked_for_the_series_poster(self):
        url, label = _discord_art_url(_Video(EPISODE))
        self.assertIn("Items/series-1/Images/Primary", url)
        self.assertIn("tag=tag-series", url)
        self.assertEqual(label, "Some Show")

    def test_the_returned_url_has_no_token_over_several_items(self):
        # Three steps, not one: a caller that only ever ran once would not
        # have caught a token appearing on the second item of a queue.
        for item in (EPISODE, MOVIE, EPISODE):
            url, _ = _discord_art_url(_Video(item))
            self.assertIsNotNone(url)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertNotIn("ApiKey", params)
            self.assertNotIn("api_key", params)

    def test_a_movie_uses_its_own_primary(self):
        url, label = _discord_art_url(_Video(MOVIE))
        self.assertIn("Items/movie-1/Images/Primary", url)
        self.assertIn("tag=tag-movie", url)
        self.assertEqual(label, "A Film")

    def test_an_episode_does_not_borrow_the_episodes_own_still(self):
        url, _ = _discord_art_url(_Video(EPISODE))
        self.assertNotIn("tag-episode", url)


class PublicUrlSettingTest(_ArtCase):
    """The way out of the LAN refusal, and the thing that makes it useful."""

    def test_the_setting_is_used_for_the_image(self):
        settings.discord_public_url = "https://jelly.example.com"
        url, _ = _discord_art_url(_Video(EPISODE, _Client()))
        self.assertTrue(url.startswith("https://jelly.example.com/"))

    def test_the_setting_rescues_a_lan_server(self):
        # The case it exists for: connected on the LAN, reachable in public.
        settings.discord_public_url = "https://jelly.example.com"
        url, label = _discord_art_url(
            _Video(EPISODE, _Client("http://192.168.2.10:8096")))
        self.assertIsNotNone(url, "a public url was configured and ignored")
        self.assertTrue(url.startswith("https://jelly.example.com/"))
        self.assertEqual(label, "Some Show")

    def test_a_configured_url_carries_no_token_either(self):
        # The public base is not a licence to append a credential: Discord
        # fetches this, and the proxy is supposed to serve it openly.
        settings.discord_public_url = "https://jelly.example.com"
        url, _ = _discord_art_url(_Video(EPISODE, _Client()))
        self.assertNotIn("ApiKey", url)
        self.assertNotIn("api_key", url)

    def test_a_lan_setting_does_not_rescue_anything(self):
        # Setting it to another private address is not a fix: Discord still
        # cannot reach it, so there is still no image.
        settings.discord_public_url = "http://10.0.0.9:8096"
        self.assertEqual(
            _discord_art_url(_Video(EPISODE, _Client())), (None, None))

    def test_trailing_slash_and_padding_do_not_double_up(self):
        settings.discord_public_url = "  https://jelly.example.com/  "
        url, _ = _discord_art_url(_Video(EPISODE))
        self.assertTrue(url.startswith("https://jelly.example.com/Items/"))
        self.assertNotIn("com//", url)

    def test_an_empty_setting_falls_back_to_the_connected_address(self):
        settings.discord_public_url = ""
        url, _ = _discord_art_url(_Video(EPISODE, _Client()))
        self.assertTrue(url.startswith("https://jelly.example/"))

    def test_the_setting_does_not_change_which_server_is_asked(self):
        # It is a base for the image request and nothing else -- the client
        # object is not touched, and playback keeps its own address.
        settings.discord_public_url = "https://jelly.example.com"
        client = _Client("http://192.168.2.10:8096")
        _discord_art_url(_Video(EPISODE, client))
        self.assertEqual(client.config.data["auth.server"],
                         "http://192.168.2.10:8096")


class NonPublicHostTest(_ArtCase):
    """Discord cannot reach these, and the ranges that look public are the
    ones worth pinning -- 172.32 is NOT private, 172.16 is."""

    def test_private_ranges_are_refused(self):
        for server in ("http://localhost:8096", "http://192.168.1.10:8096",
                       "http://10.0.0.5:8096", "http://127.0.0.1:8096",
                       "http://172.16.0.1:8096", "http://172.31.255.1:8096",
                       "http://100.64.0.1:8096",
                       "http://100.127.255.1:8096"):
            self.assertEqual(
                _discord_art_url(_Video(EPISODE, _Client(server))),
                (None, None), "%s was offered to Discord" % server)

    def test_tailscale_names_are_refused(self):
        # A real public DNS name pointing at a private tailnet host.
        for server in ("https://box.tail1234.ts.net",
                       "https://box.tail1234.ts.net:443"):
            self.assertEqual(
                _discord_art_url(_Video(EPISODE, _Client(server))),
                (None, None), "%s was offered to Discord" % server)

    def test_lookalike_addresses_are_offered(self):
        # 172.32 and 100.128 are public; refusing them would be a silent
        # feature loss for someone whose server really is there.
        for server in ("http://172.32.0.1:8096", "http://100.128.0.1:8096",
                       "https://jelly.example", "https://ts.net.example.org"):
            url, _ = _discord_art_url(_Video(EPISODE, _Client(server)))
            self.assertIsNotNone(url, "%s was refused" % server)


class FallbackTest(_ArtCase):
    """No art is answered "no art", never an exception: this runs inside the
    presence block, where raising would cost the whole update."""

    def test_no_image_tag_is_no_url(self):
        url, label = _discord_art_url(
            _Video({"Id": "x", "Type": "Movie", "Name": "Bare"}))
        self.assertIsNone(url)
        self.assertIsNone(label)

    def test_a_movie_without_its_own_id_is_not_offered(self):
        # A Primary tag with nothing to hang it on is a broken URL, not a
        # missing picture -- the only honest answer is the fallback.
        url, _ = _discord_art_url(_Video(dict(MOVIE, Id=None)))
        self.assertIsNone(url)

    def test_no_client_is_no_url(self):
        video = _Video(EPISODE)
        video.client = None
        self.assertEqual(_discord_art_url(video), (None, None))

    def test_no_item_is_no_url(self):
        self.assertEqual(_discord_art_url(_Video(None)), (None, None))

    def test_a_server_with_no_host_is_no_url(self):
        self.assertEqual(
            _discord_art_url(_Video(EPISODE, _Client(""))), (None, None))

    def test_a_client_without_config_is_no_url(self):
        video = _Video(EPISODE)
        video.client = object()
        self.assertEqual(_discord_art_url(video), (None, None))


class TmdbFallbackTest(_ArtCase):
    """The two sources, and which one wins.

    A configured TMDB lookup is tried first and the Jellyfin URL is the
    fallback -- not either/or, because TMDB has nothing for a home video or
    an obscure local-language show, and those are exactly the items whose art
    does exist on the user's own server.
    """

    def setUp(self):
        super().setUp()
        self._tmdb = (settings.discord_tmdb_enabled,
                      settings.discord_tmdb_api_key)
        settings.discord_tmdb_enabled = True
        settings.discord_tmdb_api_key = "test-key"
        tmdb_art.clear_cache()
        self.addCleanup(self._restore_tmdb)

    def _restore_tmdb(self):
        (settings.discord_tmdb_enabled,
         settings.discord_tmdb_api_key) = self._tmdb
        tmdb_art.clear_cache()

    def _patch_lookup(self, fn):
        real = tmdb_art.lookup
        self.addCleanup(lambda: setattr(tmdb_art, "lookup", real))
        tmdb_art.lookup = fn

    def test_a_tmdb_hit_is_used_instead_of_the_server_url(self):
        self._patch_lookup(lambda item: ("https://image.tmdb.org/p.jpg", "S"))
        url, label = _discord_art_url(_Video(EPISODE))
        self.assertEqual(url, "https://image.tmdb.org/p.jpg")
        self.assertEqual(label, "S")

    def test_a_tmdb_hit_rescues_a_lan_server(self):
        # The case the feature exists for: nowhere public to point Discord
        # at, and no reverse proxy, but TMDB's CDN is public by construction.
        self._patch_lookup(lambda item: ("https://image.tmdb.org/p.jpg", "S"))
        url, _ = _discord_art_url(
            _Video(EPISODE, _Client("http://192.168.2.10:8096")))
        self.assertEqual(url, "https://image.tmdb.org/p.jpg")

    def test_a_tmdb_miss_falls_back_to_the_server_url(self):
        self._patch_lookup(lambda item: (None, None))
        url, _ = _discord_art_url(_Video(EPISODE))
        self.assertIn("Items/series-1/Images/Primary", url)

    def test_a_tmdb_failure_falls_back_to_the_server_url(self):
        # A raising lookup must cost the feature, not the presence update.
        def boom(item):
            raise RuntimeError("network went away")

        self._patch_lookup(boom)
        url, _ = _discord_art_url(_Video(EPISODE))
        self.assertIn("Items/series-1/Images/Primary", url)

    def test_a_disabled_lookup_is_not_consulted(self):
        settings.discord_tmdb_enabled = False
        self._patch_lookup(
            lambda item: self.fail("TMDB was consulted while disabled"))
        url, _ = _discord_art_url(_Video(EPISODE))
        self.assertIn("Items/series-1/Images/Primary", url)

    def test_a_tmdb_url_carries_no_jellyfin_token_either(self):
        self._patch_lookup(
            lambda item: ("https://image.tmdb.org/p.jpg", "S"))
        url, _ = _discord_art_url(_Video(EPISODE))
        self.assertNotIn("ApiKey", url)
        self.assertNotIn("test-key", url)


class TmdbEndToEndTest(_ArtCase):
    """The real lookup, the real client, the real presence path.

    `TmdbFallbackTest` replaces `lookup` to test the *choice*; this one drives
    it, so the thing under test is the whole flow a film actually takes. It is
    a loop rather than a single call because the lookup is deliberately
    asynchronous: the first tick draws the Jellyfin URL and a later tick draws
    the TMDB poster, and a one-step test would call the first tick a failure
    or the second one a success without ever seeing the change.
    """

    def setUp(self):
        super().setUp()
        self._tmdb = (settings.discord_tmdb_enabled,
                      settings.discord_tmdb_api_key)
        settings.discord_tmdb_enabled = True
        settings.discord_tmdb_api_key = "test-key"
        tmdb_art.clear_cache()
        self.addCleanup(self._restore_tmdb)

    def _restore_tmdb(self):
        (settings.discord_tmdb_enabled,
         settings.discord_tmdb_api_key) = self._tmdb
        for thread in list(threading.enumerate()):
            if thread.name == "tmdb-art" and thread.is_alive():
                thread.join(timeout=10)
        tmdb_art.clear_cache()

    def _patch_get(self, payload):
        import requests
        real = requests.get

        class _Resp:
            status_code = 200

            def json(self):
                return payload

        self.addCleanup(lambda: setattr(requests, "get", real))
        requests.get = lambda *a, **k: _Resp()

    def _urls_over_ticks(self, video, ticks=6):
        """The art URL from `ticks` successive progress updates."""
        urls = []
        for _ in range(ticks):
            urls.append(_discord_art_url(video)[0])
            for thread in list(threading.enumerate()):
                if thread.name == "tmdb-art" and thread.is_alive():
                    thread.join(timeout=10)
        return urls

    def test_a_lan_server_walks_from_the_server_url_to_tmdb(self):
        self._patch_get({"poster_path": "/poster.jpg"})
        item = dict(EPISODE, ProviderIds={"Tmdb": "1399"})
        urls = self._urls_over_ticks(
            _Video(item, _Client("http://192.168.2.10:8096")))

        # With a TMDB key configured and a LAN server there is no Jellyfin URL
        # to offer at all, so the first tick is the logo and a later one is
        # the poster. Asserting the whole list catches both a lookup that
        # never settles and one that regresses to the LAN address.
        self.assertTrue(all(url is None or "image.tmdb.org" in url
                            for url in urls),
                        "a LAN address was offered to Discord: %r" % (urls,))
        self.assertIn("https://image.tmdb.org/t/p/w500/poster.jpg", urls)
        self.assertEqual(urls[-1], urls[-2],
                         "the answer must be stable once it is cached")

    def test_a_public_server_keeps_its_own_art_when_tmdb_has_none(self):
        # The fallback's whole purpose, across the tick loop rather than at
        # one instant: TMDB answering "no poster" must not cost the user the
        # artwork their own server has.
        self._patch_get({"poster_path": None})
        item = dict(EPISODE, ProviderIds={"Tmdb": "1399"})
        urls = self._urls_over_ticks(_Video(item))

        self.assertNotIn(None, urls,
                         "the Jellyfin URL was dropped for a TMDB miss")
        self.assertTrue(all("Items/series-1/Images/Primary" in url
                            for url in urls))

    def test_the_token_never_appears_over_the_whole_walk(self):
        self._patch_get({"poster_path": "/poster.jpg"})
        item = dict(EPISODE, ProviderIds={"Tmdb": "1399"})
        for url in self._urls_over_ticks(_Video(item)):
            self.assertNotIn("test-key", url)


if __name__ == "__main__":
    unittest.main()
