"""Cover art for Discord, looked up in TMDB instead of the user's server.

Discord fetches ``large_image`` from its own infrastructure, so the URL it is
given has to be reachable from the public internet. ``player_reporting``
solves that one way: build a tokenless Jellyfin image URL and refuse the ones
Discord could never fetch (a LAN address). This module is the other way, and
the two are complementary rather than alternatives -- a poster on TMDB's CDN
is public by construction, so it works for someone whose Jellyfin is a box in
a cupboard with no reverse proxy at all.

**The lookup is a request that names what the user is watching**, made to a
third party, so it is opt-in twice over: ``discord_tmdb_enabled`` *and* a key
the user pasted themselves. There is no bundled key: a shared one would be
rate-limited for every user at once, and shipping it would make this project
answerable for what other people's installs send TMDB.

**Nothing here may raise into the caller.** This runs from the progress timer
inside the presence block (see ``player_reporting.get_timeline_options``),
where an exception costs the whole presence update, so every failure is a
``None`` and every network call has a timeout. A missing poster is the Jellyfin
logo, which is what Discord showed before this feature existed.

``requests`` -- a hard dependency of the project -- is imported inside ``_get``
rather than at module scope, so a broken install costs this feature and not
the app, and so the tests that only exercise the id arithmetic import
nothing network-shaped.
"""

import logging
import threading
import time
import urllib.parse

from .conf import settings

log = logging.getLogger("tmdb_art")

#: The v3 API. `api_key` in the query string rather than a bearer header:
#: TMDB accepts both, and the query form is what its own docs use for v3.
_API_ROOT = "https://api.themoviedb.org/3"

#: Where poster files are served. A path TMDB returns -- ``/abc123.jpg`` --
#: is appended to this, at a size we choose.
_IMAGE_ROOT = "https://image.tmdb.org/t/p"

#: Discord's large asset is drawn in a small circle/square, so a full-size
#: poster is wasted bytes and slower to fetch. ``w500`` is TMDB's own usual
#: poster tier and is comfortably more than the asset needs.
_IMAGE_SIZE = "w500"

#: Short, because this is only ever a decorative image, and a presence update
#: held behind a slow search is a worse outcome than the Jellyfin logo. It no
#: longer blocks a caller at all (see ``lookup``) -- this bounds how long a
#: worker can sit on its in-flight marker before a later tick retries.
#: Tuple is (connect, read).
_TIMEOUT = (3, 5)

#: How long a lookup result is remembered. The progress timer calls this every
#: few seconds for the same item, and a search per tick would be thousands of
#: requests per film -- which is how a user's key gets rate-limited.
_CACHE_TTL = 6 * 60 * 60

#: Bounded so a long queue cannot grow the cache without limit. The key is a
#: provider id, so entries are tiny; this is a leak guard, not a size budget.
_CACHE_MAX = 512

#: ProviderIds whose value is a TMDB id. Jellyfin writes the numeric id it
#: stored from its own metadata refresh, so this is exact and needs no search.
TMDB_PROVIDER_KEYS = ("Tmdb", "TMDB")

#: ProviderIds that *identify the work* but are not TMDB ids, used only when
#: no TMDB id is present. An IMDb id can be resolved through /find; TVDB ids
#: cannot, because TMDB's /find does not accept them.
IMDB_PROVIDER_KEY = "Imdb"

_cache = {}
_cache_lock = threading.Lock()

#: Provider ids whose lookup is already in flight, so the timer does not
#: start a second identical request while the first is still running.
_in_flight = set()


def _cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    url, label, expires = entry
    if expires < time.monotonic():
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return url, label


def _cache_put(key, url, label):
    """Publish an answer and clear its in-flight marker, under one lock.

    **Both in the same critical section is the point.** Dropping the marker
    first would open a window in which a tick finds no cache entry and no
    marker, and starts a second request for a poster that is already on its
    way -- a duplicate that no amount of care at the call sites can prevent,
    because the gap is between two reads of two different structures.
    """
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Eviction by expiry scan rather than an LRU order: the entries
            # are all the same age class in practice (one film at a time),
            # and a wrong eviction only costs one extra request.
            now = time.monotonic()
            for stale in [k for k, v in _cache.items() if v[2] < now]:
                _cache.pop(stale, None)
            if len(_cache) >= _CACHE_MAX:
                _cache.clear()
        _cache[key] = (url, label, time.monotonic() + _CACHE_TTL)
        _in_flight.discard(key)


def clear_cache():
    """Drop every remembered lookup. For tests and for a key change."""
    with _cache_lock:
        _cache.clear()
        _in_flight.clear()


def _configured():
    """Whether a TMDB lookup is both switched on and usable at all."""
    return bool(settings.discord_tmdb_enabled
                and (settings.discord_tmdb_api_key or "").strip())


def _get(url, **params):
    """``json()`` for a TMDB request, or None.

    One place for the timeout, the key and the error handling, so no call
    site can forget one. ``requests`` is a hard dependency of this project,
    but it is still imported here rather than at module scope: this module is
    imported by ``player_reporting``, which several unit modules import, and
    a missing requests should cost this feature and not the app.
    """
    import requests

    params["api_key"] = settings.discord_tmdb_api_key.strip()
    language = (settings.discord_tmdb_language or "").strip()
    if language:
        params["language"] = language
    try:
        response = requests.get(url, params=params, timeout=_TIMEOUT)
        if response.status_code != 200:
            # 401 is a bad key and 429 is rate limiting; both are worth one
            # line, because the user can act on either and cannot see them.
            log.info("TMDB returned %s for %s", response.status_code,
                     urllib.parse.urlparse(url).path)
            return None
        return response.json()
    except Exception:
        log.debug("TMDB request failed:", exc_info=True)
        return None


def _provider_id(item, key):
    value = (item.get("ProviderIds") or {}).get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _search_kind(item):
    """Which TMDB namespace an id lookup should be made in.

    ``movie`` or ``tv``, i.e. which ``/find`` result list to read. A
    ``Season`` and an ``Episode`` are both ``tv`` because their ids resolve
    against the series, which is also the artwork wanted.
    """
    if item.get("Type") in ("Episode", "Series", "Season"):
        return "tv"
    return "movie"


def _tmdb_id(item):
    """``(kind, id)`` from the item's own provider ids, or ``(None, None)``.

    ``kind`` is ``movie`` or ``tv``, i.e. which TMDB namespace the id lives
    in. An episode carries the *series* id, because a poster is what is being
    looked for and an episode's own still is a screenshot.
    """
    kind = _search_kind(item)
    for key in TMDB_PROVIDER_KEYS:
        found = _provider_id(item, key)
        if found:
            return kind, found
    return None, None


def _find_by_imdb(imdb_id, kind):
    """Resolve an IMDb id through TMDB's /find, or ``(None, None)``.

    Only the namespace asked for is trusted: /find returns ``movie_results``,
    ``tv_results`` and others, and picking the wrong list is how an episode
    ends up wearing a film's poster.
    """
    if not imdb_id:
        return None, None
    result = _get("%s/find/%s" % (_API_ROOT, urllib.parse.quote(imdb_id)),
                  external_source="imdb_id")
    if not isinstance(result, dict):
        return None, None
    for entry in result.get("%s_results" % kind) or []:
        if not isinstance(entry, dict):
            continue
        found = entry.get("id")
        if found:
            return kind, str(found)
    return None, None


def _poster_path(kind, tmdb_id):
    """The poster path TMDB has for this id, or None.

    **The path is a string or it is nothing**, and the check is not
    paranoia: it is spliced into a URL, so a truthy non-string (a dict from a
    changed API, a number from a proxy's rewritten JSON) would be formatted
    into the asset URL Discord is handed -- a link to a 404 that *looks* like
    a successful lookup, which is the one failure mode this module cannot
    report, because nothing about it raises.
    """
    result = _get("%s/%s/%s" % (_API_ROOT, kind, tmdb_id))
    if not isinstance(result, dict):
        return None
    path = result.get("poster_path")
    if not path or not isinstance(path, str):
        return None
    return path


def _label(item):
    return item.get("SeriesName") or item.get("Name")


def lookup(item):
    """Public art ``(url, label)`` for a Jellyfin item, or ``(None, None)``.

    **Never blocks on the network.** A cache hit returns immediately; a miss
    starts a worker and returns ``(None, None)`` for now, so the caller falls
    through to the Jellyfin URL and gets the TMDB art on a later tick once the
    answer is cached. That is not just politeness: this is called from
    ``get_timeline_options``, which the stop path runs *before* it tears
    playback down (`player.py`'s ``stop``), so a synchronous lookup would put
    an HTTP round trip between the user pressing stop and the window actually
    clearing.

    The cache is keyed on what can be read off the item **without asking
    TMDB**, because the resolved id is exactly the thing that is not known
    yet. An item carrying a TMDB id keys on that; one carrying only an IMDb
    id keys on the IMDb id, and the ``/find`` that turns it into a TMDB id
    happens in the worker like everything else. Keying on the resolved id
    instead would re-run ``/find`` on every timer tick -- which is what the
    first version of this did.

    Silent on every failure, for the reason in the module docstring. A
    configured lookup that finds nothing is not an error -- TMDB is much
    better at films and popular series than at a home video or an obscure
    local-language show -- so the caller falls through to the Jellyfin URL,
    which is exactly the content this path cannot help with.
    """
    if not item or not _configured():
        return None, None
    try:
        cache_key = _cache_key(item)
    except Exception:
        log.debug("TMDB art lookup failed:", exc_info=True)
        return None, None
    if not cache_key:
        return None, None

    cached = _cache_get(cache_key)
    if cached is not None:
        # A negative result is cached as (None, None) and is a real answer;
        # `_cache_get` returns it as a tuple, so this is not the same as a
        # miss and must not re-queue the request.
        return cached

    _start_lookup(cache_key, _label(item))
    return None, None


def _cache_key(item):
    """A key built only from what the item already carries, or None.

    ``kind`` is in the key even when it did not come from the item: it keeps a
    film and a series that somehow share an id from sharing a poster.
    """
    kind, tmdb_id = _tmdb_id(item)
    if tmdb_id:
        return "%s:%s" % (kind, tmdb_id)
    imdb = _provider_id(item, IMDB_PROVIDER_KEY)
    if imdb:
        return "imdb:%s:%s" % (_search_kind(item), imdb)
    return None


def _start_lookup(cache_key, label):
    """Kick off the request for ``cache_key`` if one is not already running."""
    with _cache_lock:
        if cache_key in _in_flight:
            return
        _in_flight.add(cache_key)
    thread = threading.Thread(
        target=_fetch_into_cache, args=(cache_key, label),
        name="tmdb-art", daemon=True)
    thread.start()


def _fetch_into_cache(cache_key, label):
    """The worker: resolve the id, fetch a poster path, publish the answer.

    Everything that can reach the network is in here, including the ``/find``
    that turns an IMDb key into a TMDB id -- see ``lookup``. The label is
    passed rather than the item so the thread holds no reference to a
    ``Media`` that may already be off the queue by the time it finishes.
    """
    path = None
    try:
        path = _poster_path_for_key(cache_key)
    except Exception:
        log.debug("TMDB art fetch failed:", exc_info=True)
    # A failure is cached as a miss for the same reason a miss is: without it
    # the timer re-queues the same failed request every few seconds.
    url = "%s/%s%s" % (_IMAGE_ROOT, _IMAGE_SIZE, path) if path else None
    _cache_put(cache_key, url, label if url else None)


def _poster_path_for_key(cache_key):
    """Resolve ``cache_key`` and return a poster path, or None.

    The key carries enough to redo the resolution: a ``kind:id`` pair is
    already resolved, and ``imdb:kind:id`` needs the /find the synchronous
    side deliberately did not do.
    """
    if cache_key.startswith("imdb:"):
        _, kind, imdb = cache_key.split(":", 2)
        kind, tmdb_id = _find_by_imdb(imdb, kind)
        if not tmdb_id:
            return None
    else:
        kind, tmdb_id = cache_key.split(":", 1)
    return _poster_path(kind, tmdb_id)
