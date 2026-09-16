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

import unittest
import urllib.parse

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

if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()