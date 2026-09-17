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

**How an item is turned into a TMDB id.** In priority order:

1. ``ProviderIds.Tmdb``/``TMDB`` -- an exact TMDB id, so no lookup at all.
2. ``ProviderIds.Tvdb``, for TV types only, resolved through ``/find`` with
   ``external_source=tvdb_id``.
3. ``ProviderIds.Imdb``, resolved through ``/find`` with ``imdb_id``.

TVDB before IMDb is deliberate and is the TV-specific rule: Jellyfin's TVDB
provider is what fills a series' episode metadata, so a matched series nearly
always has a ``Tvdb`` id, and one that came out of the same database the
episodes did cannot disagree with them. An IMDb id is a second handle that is
often absent and occasionally points at a title TMDB files under a different
entry. TVDB is never tried for a film -- ``/find`` has no movie namespace for
it, so it would be a guaranteed round trip to a guaranteed miss.

**An episode is looked up as its series, not as itself.** That is not a
preference, it is a correction to what the Jellyfin DTO contains: an
episode's ``ProviderIds`` are the *episode's own* ids (Jellyfin's TMDB and
TVDB episode providers both write them), and TMDB has no poster for an
episode -- it has ``still_path``, a 16:9 frame grab that looks like a mistake
in Discord's square asset. `_art_item` fetches the series through the Jellyfin
client and does the whole lookup against that. Jellyfin's own image
resolution makes the same climb (``SeriesPrimaryImageTag``), and a *season's*
poster is skipped for the same reason: the series' is what the fallback shows
and what is reliably present.

**The result list is chosen per source, and the item's own namespace wins.**
``imdb_id`` answers in ``movie_results`` and ``tv_results``; ``tvdb_id``
answers in ``tv_results``, ``tv_season_results`` and ``tv_episode_results``.
Only the poster-bearing lists are read, and the item's own namespace is tried
first within whatever the source offers -- a film is a movie and a show is a
show, even though ``/find`` answers an IMDb id in both when the title exists
in both.

**Nothing here may raise into the caller.** This runs from the progress timer
inside the presence block (see ``player_reporting.get_timeline_options``),
where an exception costs the whole presence update, so every failure is a
``None`` and every network call has a timeout. A missing poster is the Jellyfin
logo, which is what Discord showed before this feature existed.

**The logging is deliberately thin: one line when the lookup finds nothing,
and warnings for the things a user can act on.** The default
``mpv_log_level`` is ``info``, so a ``debug`` line is invisible in the log of
the install that has the problem -- which is the trap this module fell into
once, where nearly every failure path was ``debug`` and the whole feature was
undiagnosable. The split:

- ``info`` -- exactly one line, on the ordinary path, when a finished lookup
  produced no art:

      No TMDB cover art for 'Some Show'; using the server's own artwork
      instead. Item: Id='...' Type='Series' ... ProviderIds={Tmdb=1399} ...

  It fires once per item because the answer is cached, and the two variants
  (the item carries no id at all, and the lookup ran but matched nothing)
  open with the same phrase on purpose: from a reader's point of view they
  are one finding, and one string to grep beats two accurate ones. The
  **success path is silent** -- a working feature is not news, and a line per
  film is noise in every log that has this switched on.
- ``warning`` -- something is wrong and the user can act on it: no API key
  with the feature on, a rejected or rate-limited request, a connection that
  failed (with the exception type, which separates "no internet" from "a
  blocker"), and any response whose shape is not what this code expects.
  Everything else that used to be logged here was removed: the per-step
  traces, the full ``/find`` response, and the "this request is already
  running" line all described a *working* lookup, or a failing one that the
  info line above already reports.

There is no ``debug`` level in this module at all.

**The item's own keys go on the "no art" lines.** These are the lines a
reader has to act on, and the first question they provoke -- "why does *this*
one have no id?" -- is answerable only from the item, with no second chance
to collect it. ``_describe_item`` writes an allowlist of the identity keys
(``ProviderIds``, ``ImageTags``, the ``Series*`` fields, and so on) with
missing ones shown as ``None``, so the absent key is visible rather than
inferred. It is deliberately not the whole DTO: a Jellyfin item is fifty-odd
keys of nested media sources, and dumping it would bury the diagnostic in the
log it is meant to be found in.

**The API key is never logged.** It travels as a query parameter, so every
line names ``urllib.parse.urlparse(url).path`` instead of the URL.

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

#: ProviderIds that *identify the work* but are not TMDB ids, and are
#: therefore resolved through /find. Order matters and is the point: TVDB
#: first for telly, IMDb second.
#:
#: **Why TVDB wins for a show.** Jellyfin's TVDB provider is what fills a
#: series' episode metadata, so when a series has been matched at all it has
#: a Tvdb id -- and an id that came from the same database the episodes did
#: cannot disagree with them. An IMDb id on a series is a *second* handle
#: that may be absent, or may point at a title TMDB files under a different
#: entry (a remake, a regional re-cut), and it costs an extra round trip
#: when TMDB already knows the answer.
#:
#: TMDB's /find takes ``tvdb_id`` for TV shows, seasons and episodes only --
#: there is no movie namespace for it -- so this is consulted for TV types
#: and never for a film.
TVDB_PROVIDER_KEY = "Tvdb"
IMDB_PROVIDER_KEY = "Imdb"

_cache = {}
_cache_lock = threading.Lock()

#: Provider ids whose lookup is already in flight, so the timer does not
#: start a second identical request while the first is still running.
_in_flight = set()

#: Items already reported as carrying no id TMDB could be asked about, so
#: that line is written once each rather than every few seconds for the whole
#: of a film. Bounded, because a long session could otherwise walk a whole
#: library through it.
_unidentified_seen = set()
_UNIDENTIFIED_MAX = 64

#: Series items already fetched, keyed by Jellyfin's series id. This is what
#: keeps an episode from costing a `get_item` on *every* progress tick -- the
#: art cache below cannot help, because it is keyed on a TMDB id that is not
#: known until the fetch has happened.
#:
#: Bounded like the art cache, and for the same reason: a long session
#: through many shows should not grow it without limit. The entries are one
#: small dict each.
_series_cache = {}

#: Whether the no-key warning has already been written. See `_configured`.
_warned_no_key = False


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
    global _warned_no_key
    with _cache_lock:
        _cache.clear()
        _in_flight.clear()
        _unidentified_seen.clear()
        _series_cache.clear()
        _warned_no_key = False


def _configured():
    """Whether a TMDB lookup is both switched on and usable at all."""
    global _warned_no_key
    if not settings.discord_tmdb_enabled:
        # Silent: the feature is off, which is not a failure and is the
        # state of every install that never asked for this.
        _warned_no_key = False
        return False
    if not (settings.discord_tmdb_api_key or "").strip():
        # NOT silent, and at warning level. `discord_tmdb_enabled` with no
        # key is a configuration that cannot work, the settings screen shows
        # both controls, and the only symptom is cover art that never
        # appears -- which looks exactly like TMDB not knowing the film.
        #
        # Once, not once per tick: this is called every few seconds for the
        # whole of a film, and a repeated warning is what teaches people to
        # ignore the warnings that matter. The flag resets when the feature
        # is switched off, so turning it back on warns again.
        if not _warned_no_key:
            _warned_no_key = True
            log.warning(
                "TMDB cover art is switched on but no API key is set "
                "(discord_tmdb_api_key); no cover art will be looked up.")
        return False
    _warned_no_key = False
    return True


def _get(url, **params):
    """``json()`` for a TMDB request, or None.

    One place for the timeout, the key and the error handling, so no call
    site can forget one. ``requests`` is a hard dependency of this project,
    but it is still imported here rather than at module scope: this module is
    imported by ``player_reporting``, which several unit modules import, and
    a missing requests should cost this feature and not the app.

    **The API key is never logged.** It travels as a query parameter, so the
    full URL would carry it -- every line below logs ``path``, which is the
    endpoint alone. The status code and the TMDB error are logged instead,
    and those are what actually distinguish the causes: 401 is a bad key, 429
    is rate limiting, 404 is an id that does not exist.
    """
    import requests

    params["api_key"] = settings.discord_tmdb_api_key.strip()
    language = (settings.discord_tmdb_language or "").strip()
    if language:
        params["language"] = language
    path = urllib.parse.urlparse(url).path
    try:
        response = requests.get(url, params=params, timeout=_TIMEOUT)
    except Exception as exc:
        # A timeout, a DNS failure and a refused connection are three
        # different user problems -- no internet, a blocker, a proxy -- and
        # the exception type is the only thing that separates them. The
        # traceback is not worth it here (this is a background thread and
        # the exception is already caught), so the repr carries the detail.
        log.warning("TMDB request failed for %s: %s: %s",
                    path, type(exc).__name__, exc)
        return None

    if response.status_code != 200:
        # The body carries TMDB's own explanation, which is the difference
        # between "you typed the key wrong" and "this account is suspended".
        log.warning("TMDB returned %s for %s: %s", response.status_code,
                    path, _error_detail(response))
        return None

    try:
        return response.json()
    except Exception as exc:
        # A proxy's HTML error page, a truncated body, a captive portal.
        # Status 200 and unparseable JSON is a middlebox, not TMDB.
        log.warning("TMDB returned unparseable JSON for %s: %s: %s",
                    path, type(exc).__name__, exc)
        return None


def _error_detail(response):
    """TMDB's own error message from a failed response, or a stand-in.

    Best-effort by design: this is a diagnostics path, and a failure to
    describe a failure must not become a second one.
    """
    try:
        body = response.json()
        message = body.get("status_message") or body.get("message")
        if message:
            return message
        return "no message (body keys: %s)" % (
            ", ".join(sorted(body)) or "none")
    except Exception:
        return "no parseable body"


def _provider_id(item, key):
    value = (item.get("ProviderIds") or {}).get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


#: Provider fields to ask Jellyfin for when fetching a series. ProviderIds is
#: what the lookup reads and is not returned by a bare get_item.
SERIES_FIELDS = "ProviderIds"

#: Item types whose own provider ids do NOT resolve to artwork worth having.
#: An episode is the case: TMDB gives an episode a ``still_path`` (a 16:9
#: screenshot), not a ``poster_path``, and a screenshot in Discord's square
#: large asset looks like a mistake. Jellyfin's own client agrees -- its
#: episode image resolution climbs to ``SeriesPrimaryImageTag``.
_CLIMBING_TYPES = ("Episode",)


def _search_kind(item):
    """Which TMDB namespace an id lookup should be made in.

    ``movie`` or ``tv``, i.e. which ``/find`` result list to read. Every
    TV-shaped type is ``tv``: an episode resolves against its series, and a
    season against its series too, because that is where the poster is.
    """
    if item.get("Type") in ("Episode", "Series", "Season"):
        return "tv"
    return "movie"


def _tmdb_id(item):
    """``(kind, id)`` from the item's own provider ids, or ``(None, None)``.

    ``kind`` is ``movie`` or ``tv``, i.e. which TMDB namespace the id lives in.
    """
    kind = _search_kind(item)
    for key in TMDB_PROVIDER_KEYS:
        found = _provider_id(item, key)
        if found:
            return kind, found
    return None, None


def _art_item(item, client):
    """The item whose artwork should be looked up.

    **An episode climbs to its series.** This is the whole reason the
    function exists. Jellyfin puts the *episode's own* provider ids on an
    episode (`TvdbEpisodeProvider`/`TmdbEpisodeProvider` both write the
    episode's external ids), and TMDB has no poster for an episode -- it has
    ``still_path``, a 16:9 frame grab. Asking about the episode therefore
    either resolves to nothing or, worse, resolves to the episode and hands
    Discord a screenshot. The series is asked about instead, which is also
    what the Jellyfin-side fallback in ``player_reporting`` does with
    ``SeriesPrimaryImageTag``.

    The series is fetched through the Jellyfin client rather than inferred
    from the episode's id, because the episode DTO carries no series-level
    provider id -- ``SeriesId`` is a Jellyfin Guid, meaningful only to this
    server. The lookup is one request per series and the client caches items,
    so an episode of a show already playing costs nothing.

    Returns the episode itself when the climb is not possible or not
    applicable, which is the previous behaviour and still sometimes right.
    """
    if item.get("Type") not in _CLIMBING_TYPES:
        return item
    return _fetch_series(item, client) or item


def _fetch_series(item, client):
    """The series item for an episode, or None.

    Returns None -- never raises -- on any problem, because the caller has a
    usable (if inferior) fallback and this runs on the progress timer.
    """
    series_id = item.get("SeriesId")
    if not series_id or client is None:
        return None
    with _cache_lock:
        if series_id in _series_cache:
            return _series_cache[series_id]
    try:
        jellyfin = getattr(client, "jellyfin", None)
        if jellyfin is None:
            return None
        series = jellyfin.get_item(series_id, fields=SERIES_FIELDS)
    except Exception:
        # Not logged: the caller falls back to the episode's own ids, and if
        # that also finds nothing the worker reports the single "no cover
        # art" line. A line here would be a second one saying the same thing.
        return None
    # The result is used as an item in its own right -- the id arithmetic
    # reads Type, Name and ProviderIds off whatever this returns -- so a
    # non-dict is not usable and is treated as a failure.
    if not isinstance(series, dict) or not series.get("ProviderIds"):
        return None
    with _cache_lock:
        if len(_series_cache) >= _CACHE_MAX:
            _series_cache.clear()
        _series_cache[series_id] = series
    return series


#: The /find ``external_source`` per provider key, and which TMDB result
#: lists that source can answer in.
#:
#: **The lists are per source; the order between them is per item type.** The
#: two are easy to conflate and the difference is a wrong poster: ``imdb_id``
#: can answer in ``movie_results`` and ``tv_results``, so reading a fixed
#: order would hand a film the TV entry whenever TMDB happens to return both
#: -- which it does, because the same title exists in both namespaces often
#: enough. `_result_lists_for` puts the item's own namespace first and keeps
#: the other as a fallback, so a film is a movie and a show is a show without
#: either losing the ability to resolve a mislabelled id.
#:
#: ``tvdb_id`` is TV-only -- /find has no movie namespace for it -- which is
#: why a film is never offered this source at all.
_FIND_SOURCES = {
    TVDB_PROVIDER_KEY: ("tvdb_id", ("tv_results",)),
    IMDB_PROVIDER_KEY: ("imdb_id", ("movie_results", "tv_results")),
}

#: Result lists whose entries carry a ``poster_path``. ``tv_season_results``
#: and ``tv_episode_results`` are not here on purpose: they answer with a
#: season or an episode, whose poster is its own key art rather than the
#: series', and taking one is how an episode ends up wearing the wrong image.
_POSTER_RESULT_LISTS = ("tv_results", "movie_results")


def _result_lists_for(source_lists, kind):
    """``source_lists`` reordered so ``kind``'s own namespace comes first.

    A later list is still tried, so an id TMDB has filed only under the other
    namespace still resolves. What changes is which one wins when both answer.
    """
    preferred = "%s_results" % kind
    ordered = [name for name in source_lists if name == preferred]
    ordered += [name for name in source_lists if name != preferred]
    return [name for name in ordered if name in _POSTER_RESULT_LISTS]


def _find_by_external_id(external_id, source, result_lists):
    """Resolve an external id through TMDB's /find, or ``(None, None)``.

    ``result_lists`` is ordered, and the first list with a usable entry wins.
    The caller builds that order -- see ``_result_lists_for``.

    Returns ``(kind, tmdb_id)``, where ``kind`` is the namespace the id was
    found in (``tv`` or ``movie``), not the one that was asked for.
    """
    if not external_id:
        return None, None
    result = _get("%s/find/%s" % (_API_ROOT, urllib.parse.quote(external_id)),
                  external_source=source)
    if not isinstance(result, dict):
        # _get has already logged whatever it hit; a None here means the
        # request failed and a non-dict means TMDB changed shape. The second
        # case is worth naming because it is not a network problem and the
        # request line above succeeded.
        if result is not None:
            log.warning("/find (%s) for %s returned %s, not an object.",
                        source, external_id, type(result).__name__)
        return None, None
    for result_list in result_lists:
        for entry in result.get(result_list) or []:
            if not isinstance(entry, dict):
                continue
            found = entry.get("id")
            if found:
                kind = "tv" if result_list.startswith("tv") else "movie"
                return kind, str(found)
    # A successful /find with nothing usable. Common and real: TMDB's mapping
    # from TVDB and IMDb ids is not complete, and the entries it does answer
    # with can be the season/episode lists, which carry no poster. Not logged
    # here -- the caller reports the outcome once it knows whether *any* route
    # produced art, which is the line worth having.
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
        if result is not None:
            log.warning("TMDB %s/%s returned %s, not an object.",
                        kind, tmdb_id, type(result).__name__)
        return None
    path = result.get("poster_path")
    if path and not isinstance(path, str):
        # The exact case the type check above exists for, and the one a
        # reader would otherwise never see: art silently missing, with a
        # perfectly good 200 in the log.
        log.warning("TMDB %s/%s returned a %s poster_path, not a string.",
                    kind, tmdb_id, type(path).__name__)
        return None
    if not path:
        # 200 and a known title with no poster on TMDB. Not logged here --
        # the worker reports the outcome once, with the item attached.
        return None
    return path


def _label(item):
    return item.get("SeriesName") or item.get("Name")


def lookup(item, client=None):
    """Public art ``(url, label)`` for a Jellyfin item, or ``(None, None)``.

    ``client`` is the Jellyfin client for the item, used only to fetch an
    episode's series -- see ``_art_item``. It is optional because everything
    except an episode works without it.

    **Never blocks on the network** -- with one deliberate exception. A cache
    hit returns immediately; a miss starts a worker and returns
    ``(None, None)`` for now, so the caller falls through to the Jellyfin URL
    and gets the TMDB art on a later tick once the answer is cached. That is
    not just politeness: this is called from ``get_timeline_options``, which
    the stop path runs *before* it tears playback down (`player.py`'s
    ``stop``), so a synchronous lookup would put an HTTP round trip between
    the user pressing stop and the window actually clearing.

    The exception is the series fetch for an episode, which is a **local**
    request -- one ``get_item`` against the user's own server, cached by the
    client -- not one to TMDB. It is also memoised below, so it happens once
    per series rather than once per tick.

    The cache is keyed on what can be read off the *art item* without asking
    TMDB, because the resolved id is exactly the thing that is not known yet.
    An item carrying a TMDB id keys on that; one carrying only a TVDB or IMDb
    id keys on that id, and the ``/find`` that turns it into a TMDB id happens
    in the worker like everything else. Keying on the resolved id instead
    would re-run ``/find`` on every timer tick -- which is what the first
    version of this did.

    Silent on every failure, for the reason in the module docstring. A
    configured lookup that finds nothing is not an error -- TMDB is much
    better at films and popular series than at a home video or an obscure
    local-language show -- so the caller falls through to the Jellyfin URL,
    which is exactly the content this path cannot help with.
    """
    if not item or not _configured():
        return None, None
    try:
        art_item = _art_item(item, client)
        cache_key = _cache_key(art_item)
    except Exception:
        log.warning("TMDB art lookup failed to read the item:",
                    exc_info=True)
        return None, None
    if not cache_key:
        # The most common reason there is no art, and the one that looks
        # identical to a TMDB failure: nothing on the item identifies it.
        # Jellyfin only fills ProviderIds when its own metadata lookup ran,
        # which it has not for a personal recording or a file nobody
        # matched. Logged once per item type rather than per tick, because
        # this is called every few seconds. The *art* item is described, so
        # an episode with an unmatched series reports the series.
        _log_unidentified(art_item)
        return None, None

    cached = _cache_get(cache_key)
    if cached is not None:
        # A negative result is cached as (None, None) and is a real answer;
        # `_cache_get` returns it as a tuple, so this is not the same as a
        # miss and must not re-queue the request.
        return cached

    _start_lookup(cache_key, _label(art_item), _describe_item(art_item))
    return None, None


def _log_unidentified(item):
    """Say once that an item carries no id TMDB could be asked about.

    The item's identifying keys are written out with it. The question this
    line provokes -- "why does *this* one have no id?" -- is answerable only
    from the item, and there is no second chance to collect it: the timer
    calls this again seconds later with the same object and the same result.

    **An allowlist, not the whole dict.** A Jellyfin item is fifty-odd keys
    of nested DTO  -- ``MediaSources`` alone can be kilobytes -- and dumping
    it would bury the diagnostic in the log it is meant to be found in. These
    are the keys that decide the lookup, plus the ids and collections whose
    *absence* is the thing being reported.
    """
    name = item.get("SeriesName") or item.get("Name") or "<unnamed>"
    marker = "%s:%s" % (item.get("Type") or "<no type>",
                        item.get("Id") or name)
    with _cache_lock:
        if marker in _unidentified_seen or len(_unidentified_seen) >= _UNIDENTIFIED_MAX:
            return
        _unidentified_seen.add(marker)
    # Same opening phrase as the worker's line, because the two are the same
    # finding from a reader's point of view -- there is no cover art -- and
    # one string to grep is worth more than two accurate ones.
    log.info("No TMDB cover art for %r; the item carries no TMDB, TVDB or "
             "IMDb id to look up. Item: %s", name, _describe_item(item))


#: The item keys worth writing down. The lookup reads ``Type`` and
#: ``ProviderIds``; the rest are here because they are what a reader checks
#: next -- whether the server gave the item an id at all (``Id``), whether it
#: was matched to a series (``SeriesId``), and whether art exists under some
#: other name (``ImageTags``, ``PrimaryImageTag``). ``SeriesName`` and ``Name``
#: are the label, and ``ProductionYear`` distinguishes two films of one title.
_DESCRIBED_KEYS = (
    "Id", "Type", "Name", "SeriesName", "SeriesId", "ProductionYear",
    "IndexNumber", "ParentIndexNumber", "ProviderIds", "ImageTags",
    "BackdropImageTags", "PrimaryImageTag", "SeriesPrimaryImageTag",
)

def _describe_item(item):
    """The diagnostic keys of ``item`` as one line, showing what is missing.

    Missing keys are written as ``None`` rather than omitted, because the
    whole question is which of them is *not* there -- a line that lists only
    what the item has makes the reader compare against a list they are
    holding in their head.
    """
    parts = []
    for key in _DESCRIBED_KEYS:
        parts.append("%s=%s" % (key, _describe_value(item.get(key, None))))
    return " ".join(parts)


#: How many entries of a nested dict to spell out before summarising the rest.
#: `ImageTags` legitimately has a handful; a plugin can hang dozens off it, and
#: the line is a diagnostic, not a payload.
_DESCRIBED_DICT_KEYS = 8


def _describe_value(value):
    """One item field, rendered for a log line.

    Nested dicts are flattened rather than left as a Python repr, because the
    two that matter -- ``ProviderIds`` and ``ImageTags`` -- are the ones the
    reader is actually scanning for, and ``{}`` for an empty one is itself the
    answer ("the server sent a dict, and it was empty"), distinct from the
    plain ``None`` of a key that was never there at all.
    """
    if isinstance(value, dict):
        keys = sorted(value)
        shown = ", ".join("%s=%s" % (k, value[k])
                          for k in keys[:_DESCRIBED_DICT_KEYS])
        if len(keys) > _DESCRIBED_DICT_KEYS:
            shown += ", ... and %d more" % (
                len(keys) - _DESCRIBED_DICT_KEYS)
        return "{%s}" % shown
    if isinstance(value, list):
        return "[%d items]" % len(value)
    return repr(value)


#: Cache-key prefix for an id that still has to be resolved through /find.
#: The source is part of the key, so the same numeric id arriving as both a
#: TVDB and an IMDb id is two entries rather than one wrong answer.
_EXTERNAL_PREFIX = "find"


def _cache_key(item):
    """A key built only from what the item already carries, or None.

    ``kind`` is in the key even when it did not come from the item: it keeps a
    film and a series that somehow share an id from sharing a poster.

    The provider order here is the one ``_external_source`` defines: an exact
    TMDB id beats anything that needs a lookup, and within the lookups TVDB
    beats IMDb for a show.
    """
    kind, tmdb_id = _tmdb_id(item)
    if tmdb_id:
        return "%s:%s" % (kind, tmdb_id)
    for provider_key, external_id in _external_ids(item):
        return "%s:%s:%s:%s" % (_EXTERNAL_PREFIX, provider_key,
                                _search_kind(item), external_id)
    return None


def _external_ids(item):
    """``(provider_key, id)`` pairs to try, in priority order.

    TVDB first for anything TV-shaped, IMDb after -- see
    ``TVDB_PROVIDER_KEY`` for why. A movie is never offered the TVDB source,
    because /find has no movie namespace for it and asking would be a
    guaranteed round trip to a guaranteed miss.
    """
    is_tv = _search_kind(item) == "tv"
    if is_tv:
        tvdb = _provider_id(item, TVDB_PROVIDER_KEY)
        if tvdb:
            yield TVDB_PROVIDER_KEY, tvdb
    imdb = _provider_id(item, IMDB_PROVIDER_KEY)
    if imdb:
        yield IMDB_PROVIDER_KEY, imdb


def _start_lookup(cache_key, label, describe):
    """Kick off the request for ``cache_key`` if one is not already running."""
    with _cache_lock:
        if cache_key in _in_flight:
            return
        _in_flight.add(cache_key)
    thread = threading.Thread(
        target=_fetch_into_cache, args=(cache_key, label, describe),
        name="tmdb-art", daemon=True)
    thread.start()


def _fetch_into_cache(cache_key, label, describe):
    """The worker: resolve the id, fetch a poster path, publish the answer.

    Everything that can reach the network is in here, including the ``/find``
    that turns an IMDb key into a TMDB id -- see ``lookup``. Neither the item
    nor the ``Media`` around it is passed in: a ``describe`` string is
    computed up front instead, so the thread holds no reference to an object
    that may already be off the queue by the time it finishes.
    """
    path = None
    try:
        path = _poster_path_for_key(cache_key)
    except Exception as exc:
        # Should be unreachable -- every step below swallows its own
        # failures -- so this is the line that says a bug got through, with
        # the traceback that identifies it.
        log.warning("TMDB art fetch for %s raised %s:",
                    cache_key, type(exc).__name__, exc_info=True)
    # A failure is cached as a miss for the same reason a miss is: without it
    # the timer re-queues the same failed request every few seconds.
    url = "%s/%s%s" % (_IMAGE_ROOT, _IMAGE_SIZE, path) if path else None
    if not url:
        # The one line worth having on the ordinary path: the lookup finished
        # and produced nothing, which is otherwise indistinguishable from the
        # feature being off, from the item being unidentified, and from the
        # request never having been made. The item's keys go with it, because
        # the question it provokes -- "why does *this* one have no art?" --
        # is answerable only from the item, and there is no second chance to
        # collect it.
        #
        # The success path is deliberately silent: a working feature is not
        # news, and a line per film would be noise in every log that has this
        # switched on.
        log.info("No TMDB cover art for %r; using the server's own artwork "
                 "instead. Item: %s", label, describe)
    _cache_put(cache_key, url, label if url else None)


def _poster_path_for_key(cache_key):
    """Resolve ``cache_key`` and return a poster path, or None.

    The key carries enough to redo the resolution: a ``kind:id`` pair is
    already resolved, and ``find:<provider>:<kind>:<id>`` still needs the
    /find the synchronous side deliberately did not do.
    """
    if cache_key.startswith(_EXTERNAL_PREFIX + ":"):
        _, provider_key, kind, external_id = cache_key.split(":", 3)
        if provider_key not in _FIND_SOURCES:
            # Only reachable if a cache key outlives the provider table that
            # built it. The worker must not raise for that -- it is a cache
            # miss, not a fault, and the answer is the same.
            log.warning("No /find source for provider %r.", provider_key)
            return None
        source, source_lists = _FIND_SOURCES[provider_key]
        kind, tmdb_id = _find_by_external_id(
            external_id, source, _result_lists_for(source_lists, kind))
        if not tmdb_id:
            return None
    else:
        kind, tmdb_id = cache_key.split(":", 1)
    return _poster_path(kind, tmdb_id)
