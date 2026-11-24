"""VK Music provider for Music Assistant 2.10.9.

Uses a token obtained by the interactive VK setup flow. The provider does not
store VK passwords or bypass service access controls.
"""
from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING

from music_assistant.models.music_provider import MusicProvider
from music_assistant_models.enums import ContentType, ImageType, MediaType, ProviderFeature, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError, SetupFailedError
from music_assistant_models.media_items import (
    Album,
    AudioFormat,
    Artist,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

# vkpymusic is vendored in ./vendor (patched fork; fresh installs need no PyPI
# download). The fork uses only relative imports internally, so it is imported
# here as a subpackage instead of shadowing a top-level ``vkpymusic`` module.
from .vendor.vkpymusic import Service
from .vendor.vkpymusic.models import Playlist as VKPlaylist
from .vendor.vkpymusic.models import Song as VKSong
from .vendor.vkpymusic.vk_api import VkApiException
from .vendor.vkpymusic.vk_api.vk_api_extensions import make_request_async
from .vendor.vkpymusic.vk_api.vk_api_request_builder import VkApiRequestBuilder

# VK catalog.getAudio understands only ?section=... links; ?catalog=...,
# vk.cc shortlinks, ?popup=... and /artist pages silently fall back to the
# default "Главная" digest, so they must not become browse folders.
_CATALOG_LINK_PREFIX = "https://vk.com/audio?section="

if TYPE_CHECKING:
    from collections.abc import Sequence
    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import MediaItemType
    from music_assistant_models.provider import ProviderManifest

CONF_TOKEN = "token"
CONF_USER_AGENT = "user_agent"
SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.BROWSE,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize the provider and its secure account configuration."""
    return VKMusicProvider(mass, manifest, config, SUPPORTED_FEATURES)


class VKMusicProvider(MusicProvider):
    """VK track search, artist discovery and playlist/album browsing."""

    _service: Service

    @property
    def is_streaming_provider(self) -> bool:
        """VK is an online music provider."""
        return True

    @property
    def max_concurrent_streams(self) -> int:
        """Use a conservative per-account stream limit."""
        return 2

    async def handle_async_init(self) -> None:
        """Create the API client and validate the saved token."""
        token = str(self.get_setup_value(CONF_TOKEN) or "")
        user_agent = str(self.get_setup_value(CONF_USER_AGENT) or "")
        if not token or not user_agent:
            raise LoginFailed("VK access token and User-Agent are required")
        self._service = Service(user_agent=user_agent, token=token)
        self._page_cache: dict[str, tuple] = {}
        # book_id -> raw audiobook dict (with chapters), refreshed on each
        # browse of a section containing audio_books blocks
        self._book_cache: dict[str, dict] = {}
        # podcast owner_id -> raw show dict, refreshed on each browse of a
        # section containing podcasts blocks
        self._podcast_cache: dict[str, dict] = {}
        # podcast episode item_id -> (fresh signed stream url, fetched_at)
        # (audio.getById does not serve podcast episodes on this surface)
        self._episode_cache: dict[str, tuple[str, float]] = {}
        # chapter item_id -> (stream url, duration, title, fetched_at);
        # urls are signed and expire, so get_stream_details refreshes stale
        # entries before playback
        self._chapter_cache: dict[str, tuple[str, int, str, float]] = {}
        try:
            valid = await self._service.is_token_valid_async()
        except Exception as err:
            raise SetupFailedError(
                f"Could not validate VK credentials ({type(err).__name__})"
            ) from err
        if not valid:
            raise LoginFailed("VK access token is invalid or expired")

    def _mapping(self, item_id: str) -> set[ProviderMapping]:
        return {
            ProviderMapping(
                item_id=item_id,
                provider_domain=self.domain,
                provider_instance=self.instance_id,
            )
        }

    @staticmethod
    def _encode_collection_id(entry, media_type: MediaType) -> str:
        """Encode VK collection identifiers and metadata without exposing delimiters."""
        payload = {
            "owner": str(entry.owner_id),
            "playlist": str(entry.playlist_id),
            "key": str(entry.access_key),
            "name": str(entry.title or ""),
            "description": str(getattr(entry, "description", "") or ""),
            "photo": str(getattr(entry, "photo", "") or ""),
            "type": media_type.value,
        }
        packed = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        return f"vk:{packed}"

    @staticmethod
    def _decode_collection_id(item_id: str) -> dict[str, str]:
        """Decode a VK collection id and its cached descriptive metadata."""
        try:
            packed = item_id.removeprefix("vk:")
            packed += "=" * (-len(packed) % 4)
            payload = json.loads(base64.urlsafe_b64decode(packed).decode())
            if not payload.get("owner") or not payload.get("playlist") or "key" not in payload:
                raise ValueError("missing collection fields")
            return payload
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
            raise MediaNotFoundError("Invalid VK collection identifier") from err

    def _parse_song(self, song, thumb: str | None = None) -> Track:
        """Convert VK audio metadata into a Music Assistant track."""
        item_id = f"{song.owner_id}_{song.track_id}"
        artist_items = UniqueList(
            [
                ItemMapping(
                    item_id=f"artist:{song.artist.casefold()}",
                    provider=self.instance_id,
                    name=song.artist,
                    media_type=MediaType.ARTIST,
                )
            ]
            if song.artist
            else []
        )
        track = Track(
            item_id=item_id,
            provider=self.instance_id,
            name=song.title or "Unknown title",
            artists=artist_items,
            duration=max(0, int(song.duration or 0)),
            provider_mappings=self._mapping(item_id),
        )
        if thumb:
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return track

    @staticmethod
    def _thumb_from_raw(raw: dict) -> str | None:
        """Extract a cover url from a raw VK audio dict (album.thumb)."""
        album = raw.get("album")
        if isinstance(album, dict):
            thumb = album.get("thumb")
            if isinstance(thumb, dict):
                for key in ("photo_600", "photo_300", "photo_68"):
                    if thumb.get(key):
                        return str(thumb[key])
        return None

    def _parse_raw_audios(self, items: list) -> list[Track]:
        """Parse raw VK audio dicts into tracks, keeping album covers."""
        tracks: list[Track] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            try:
                song = VKSong.from_json(raw)
            except (KeyError, TypeError, ValueError):
                # audio.get can return entries without url/access
                continue
            tracks.append(self._parse_song(song, self._thumb_from_raw(raw)))
        return tracks

    async def _request_data(self, request) -> dict:
        """Execute a prepared VK API request and return its raw data dict."""
        self._service.fill_user_agent_and_token(request)
        response = await make_request_async(request)
        data = response.data
        return data if isinstance(data, dict) else {}

    def _parse_artist(self, name: str, image: str | None = None) -> Artist:
        """Represent a VK artist by a normalized search identity."""
        item_id = f"artist:{name.casefold()}"
        artist = Artist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            provider_mappings=self._mapping(item_id),
        )
        if image:
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return artist

    def _parse_vk_collection(self, entry, media_type: MediaType) -> Album | Playlist:
        """Convert the VK library's playlist-shaped album and playlist objects."""
        item_id = self._encode_collection_id(entry, media_type)
        mapping = self._mapping(item_id)
        if media_type == MediaType.ALBUM:
            item = Album(
                item_id=item_id,
                provider=self.instance_id,
                name=entry.title or "Untitled album",
                provider_mappings=mapping,
            )
        else:
            item = Playlist(
                item_id=item_id,
                provider=self.instance_id,
                name=entry.title or "Untitled playlist",
                owner=str(entry.owner_id),
                provider_mappings=mapping,
            )
        if description := getattr(entry, "description", None):
            item.metadata.description = description
        if photo := getattr(entry, "photo", None):
            item.metadata.images = UniqueList(
                [MediaItemImage(type=ImageType.THUMB, path=photo, provider=self.instance_id, remotely_accessible=True)]
            )
        return item

    @staticmethod
    def _best_image_url(images: list) -> str | None:
        """Pick the largest url from a VK image list ({url, width, ...})."""
        best = max(
            (i for i in images if isinstance(i, dict) and i.get("url")),
            key=lambda i: i.get("width") or 0,
            default=None,
        )
        return str(best["url"]) if best else None

    async def _search_raw_items(self, api_method: str, query: str, count: int) -> list[dict]:
        """Run a VK audio.* search method and return its raw item dicts."""
        request = VkApiRequestBuilder.build_from_base_request(
            method="get",
            url=api_method,
            params={"q": query, "count": count, "offset": 0},
        )
        data = await self._request_data(request)
        return [i for i in data.get("items", []) if isinstance(i, dict)]

    async def search(
        self, search_query: str, media_types: list[MediaType], limit: int = 5
    ) -> SearchResults:
        """Search VK tracks (with covers), artists, albums and playlists."""
        results = SearchResults()
        query = search_query.strip()
        if not query:
            return results
        safe_limit = min(max(limit, 1), 30)

        if MediaType.TRACK in media_types:
            try:
                raw_items = await self._search_raw_items(
                    "audio.search", query, safe_limit
                )
                results.tracks = self._parse_raw_audios(raw_items)
            except VkApiException as err:
                self.logger.warning(
                    "VK track search denied or failed (code %s)",
                    getattr(err, "error_code", "unknown"),
                )
            except Exception as err:
                self.logger.warning(
                    "Unexpected VK track search failure (%s)", type(err).__name__
                )

        if MediaType.ARTIST in media_types:
            try:
                raw_items = await self._search_raw_items(
                    "audio.searchArtists", query, safe_limit
                )
                artists: list[Artist] = []
                for item in raw_items:
                    name = self._clean_title(item.get("name"))
                    if not name:
                        continue
                    artists.append(self._parse_artist(name, self._best_image_url(item.get("photo", []))))
                results.artists = artists
            except VkApiException as err:
                self.logger.warning(
                    "VK artist search denied or failed (code %s)",
                    getattr(err, "error_code", "unknown"),
                )
            except Exception as err:
                self.logger.warning(
                    "Unexpected VK artist search failure (%s)", type(err).__name__
                )

        for media_type, api_method in (
            (MediaType.ALBUM, "audio.searchAlbums"),
            (MediaType.PLAYLIST, "audio.searchPlaylists"),
        ):
            if media_type not in media_types:
                continue
            try:
                raw_items = await self._search_raw_items(api_method, query, safe_limit)
                collections: list[Album | Playlist] = []
                for entry in raw_items:
                    try:
                        collections.append(
                            self._parse_vk_collection(
                                VKPlaylist.from_json(entry), media_type
                            )
                        )
                    except (KeyError, TypeError, ValueError):
                        continue
                if media_type == MediaType.ALBUM:
                    results.albums = collections
                else:
                    results.playlists = collections
            except VkApiException as err:
                self.logger.warning(
                    "VK %s search denied or failed (code %s)",
                    media_type.value,
                    getattr(err, "error_code", "unknown"),
                )
            except Exception as err:
                self.logger.warning(
                    "Unexpected VK %s search failure (%s)",
                    media_type.value,
                    type(err).__name__,
                )
        return results

    _STREAM_URL_TTL = 6 * 3600  # VK signed urls live well beyond this

    async def get_track(self, prov_track_id: str) -> Track:
        """Retrieve current metadata for a VK track."""
        if prov_track_id.startswith("abch:"):
            # audiobook chapter: metadata comes from the cached chapter list
            cached = self._chapter_cache.get(prov_track_id)
            if cached is None:
                book_id = prov_track_id.split(":")[1]
                book = await self._refresh_book(book_id)
                if book:
                    self._render_book_chapters(book)
                cached = self._chapter_cache.get(prov_track_id)
            if cached is None:
                raise MediaNotFoundError(f"VK audiobook chapter not found: {prov_track_id}")
            _url, duration, title = cached[:3]
            return Track(
                item_id=prov_track_id,
                provider=self.instance_id,
                name=title,
                duration=duration,
                provider_mappings=self._mapping(prov_track_id),
            )
        cached_episode = self._episode_cache.get(prov_track_id)
        if cached_episode is not None:
            # podcast episode: audio.getById does not serve episodes on this
            # token surface — rebuild metadata from the cached stream url
            return Track(
                item_id=prov_track_id,
                provider=self.instance_id,
                name=prov_track_id,
                duration=0,
                provider_mappings=self._mapping(prov_track_id),
            )
        owner, separator, track = prov_track_id.partition("_")
        if not separator or not owner or not track:
            raise MediaNotFoundError(f"Invalid VK track id: {prov_track_id}")
        songs = await self._service.get_songs_by_id_async([f"{owner}_{track}"])
        if not songs:
            raise MediaNotFoundError(f"VK track not found: {prov_track_id}")
        return self._parse_song(songs[0])

    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Resolve an artist identity referenced by VK tracks."""
        name = prov_artist_id.removeprefix("artist:").strip()
        if not name:
            raise MediaNotFoundError("Invalid VK artist identifier")
        return self._parse_artist(name)

    def _apply_collection_metadata(self, item: Album | Playlist, payload: dict[str, str]) -> None:
        """Restore collection text and artwork saved with a provider item id."""
        if description := payload.get("description"):
            item.metadata.description = description
        if photo := payload.get("photo"):
            item.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=photo,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

    async def get_album(self, prov_album_id: str) -> Album:
        """Resolve an album selected from VK search."""
        payload = self._decode_collection_id(prov_album_id)
        item = Album(
            item_id=prov_album_id,
            provider=self.instance_id,
            name=payload.get("name") or f"VK album {payload['playlist']}",
            provider_mappings=self._mapping(prov_album_id),
        )
        self._apply_collection_metadata(item, payload)
        return item

    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Resolve a playlist selected from VK search or the audiobooks catalog."""
        if prov_playlist_id.startswith("book:"):
            book = await self._get_book(prov_playlist_id)
            return self._parse_book_playlist(book)
        if prov_playlist_id.startswith("podcast:"):
            podcast = await self._get_podcast(prov_playlist_id)
            return self._parse_podcast_playlist(podcast)
        payload = self._decode_collection_id(prov_playlist_id)
        item = Playlist(
            item_id=prov_playlist_id,
            provider=self.instance_id,
            name=payload.get("name") or f"VK playlist {payload['playlist']}",
            owner=payload["owner"],
            provider_mappings=self._mapping(prov_playlist_id),
        )
        self._apply_collection_metadata(item, payload)
        return item

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Fetch the tracks belonging to a VK album."""
        return await self._fetch_collection_tracks(prov_album_id, offset=0)

    async def _fetch_collection_tracks(self, prov_collection_id: str, offset: int = 0) -> list[Track]:
        """Fetch one page of a VK playlist/album with cover-preserving parsing."""
        payload = self._decode_collection_id(prov_collection_id)
        owner, playlist, access_key = int(payload['owner']), int(payload['playlist']), payload['key']
        request = VkApiRequestBuilder.build_req_get(
            owner, count=100, offset=offset, playlist_id=playlist, access_key=access_key
        )
        data = await self._request_data(request)
        return self._parse_raw_audios(
            [i for i in data.get("items", []) if isinstance(i, dict)]
        )

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Fetch one page of a VK playlist."""
        if prov_playlist_id.startswith("book:"):
            # audiobook: chapters arrive whole (page 0 only)
            if page:
                return []
            book = await self._get_book(prov_playlist_id)
            return self._render_book_chapters(book)
        if prov_playlist_id.startswith("podcast:"):
            # podcast episodes via the app's podcasts.getEpisodes
            return await self._browse_podcast_episodes(
                prov_playlist_id.removeprefix("podcast:")
            )
        return await self._fetch_collection_tracks(prov_playlist_id, offset=page * 100)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Search VK albums using the artist name."""
        name = prov_artist_id.removeprefix("artist:")
        entries = await self._service.search_albums_by_text_async(name, count=30)
        return [self._parse_vk_collection(item, MediaType.ALBUM) for item in entries]

    async def get_artist_toptracks(self, prov_artist_id: str, limit: int = 25) -> list[Track]:
        """Search for the artist's tracks."""
        name = prov_artist_id.removeprefix("artist:")
        songs = await self._service.search_songs_by_text_async(name, count=min(limit, 30))
        return [self._parse_song(song) for song in songs]

    # ------------------------------------------------------------------
    # VK catalog (catalog.getAudio / catalog.getBlockItems) navigation.
    #
    # The catalog is a lazy tree of "digests": every catalog.getAudio call
    # returns all section tabs plus the resolved content (blocks + object
    # pools) of exactly one section (id == catalog.default_section).
    # Descending one level = one more HTTP request with the url of the
    # next node (section url, or a link url from a links block).
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_url_token(url: str) -> str:
        """Pack a catalog url into a browse-safe opaque token."""
        return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")

    @staticmethod
    def _decode_url_token(token: str) -> str:
        """Restore a catalog url from its browse token."""
        try:
            padded = token + "=" * (-len(token) % 4)
            return base64.urlsafe_b64decode(padded).decode()
        except (UnicodeDecodeError, ValueError) as err:
            raise MediaNotFoundError("Invalid VK catalog reference") from err

    @staticmethod
    def _clean_title(title: str | None) -> str:
        """Normalize VK catalog titles (non-breaking spaces)."""
        return (title or "").replace("\xa0", " ").strip()

    @staticmethod
    def _is_catalog_link(url: str) -> bool:
        """Only ?section=... links resolve through catalog.getAudio."""
        return bool(url) and url.startswith(_CATALOG_LINK_PREFIX)

    def _catalog_folder(self, item_id: str, name: str, path: str | None = None) -> BrowseFolder:
        """Build a browse folder; `path` is the FULL navigation route
        (``instance_id://segment/segment``) — the frontend browses it verbatim,
        and the websocket handler splits on ``://``."""
        full_path = f"{self.instance_id}://{path}" if path else None
        return BrowseFolder(item_id=item_id, provider=self.instance_id, name=name, path=full_path)

    async def _catalog_request(self, api_method: str, params: dict) -> dict:
        """Call a VK catalog method through the pinned upstream client."""
        request = VkApiRequestBuilder.build_from_base_request(
            method="get", url=api_method, params=dict(params)
        )
        self._service.fill_user_agent_and_token(request)
        response = await make_request_async(request)
        return response.data

    async def _catalog_digest(self, url: str | None = None) -> dict:
        """Fetch one catalog.getAudio digest for a section url."""
        params: dict = {"need_blocks": 1}
        if url:
            params["url"] = url
        return await self._catalog_request("catalog.getAudio", params)

    async def _catalog_block_items(self, block_id: str, start_from: str | None = None) -> dict:
        """Fetch one paginated page of a catalog block."""
        params: dict = {"block_id": block_id}
        if start_from:
            params["start_from"] = start_from
        return await self._catalog_request("catalog.getBlockItems", params)

    def _find_link(self, digest: dict, title: str) -> dict | None:
        """Find a catalog link by its (cleaned) title in a digest's links pool."""
        for pool_link in digest.get("links", []):
            if self._clean_title(pool_link.get("title")) == title:
                return pool_link
        return None

    def _parse_book_playlist(self, book: dict) -> Playlist:
        """Render an audiobook as a playlist item (cover + title)."""
        book_id = str(book.get("id"))
        item_id = f"book:{book_id}"
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=self._clean_title(book.get("title")) or f"Книга {book_id}",
            owner=self.instance_id,
            provider_mappings=self._mapping(item_id),
        )
        cover = sorted(
            (c for c in book.get("cover", []) if c.get("url")),
            key=lambda c: c.get("width") or 0,
            reverse=True,
        )
        if cover:
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=str(cover[0]["url"]),
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return playlist

    async def _refresh_book(self, book_id: str) -> dict | None:
        """Re-fetch the audiobooks digest to restore a book's chapters."""
        try:
            digest = await self._catalog_digest(
                "https://vk.com/audio?section=audiobooks_favorites&block=audiobooks_favorites"
            )
        except Exception:
            return None
        for book in digest.get("audio_books", []):
            if book.get("chapters"):
                self._book_cache[str(book.get("id"))] = book
        return self._book_cache.get(book_id)

    async def _get_book(self, prov_playlist_id: str) -> dict:
        """Resolve a book:<id> playlist reference to its raw audiobook dict."""
        book_id = prov_playlist_id.removeprefix("book:")
        book = self._book_cache.get(book_id) or await self._refresh_book(book_id)
        if book is None:
            raise MediaNotFoundError(f"VK audiobook not found: {book_id}")
        return book

    def _parse_podcast_playlist(self, podcast: dict) -> Playlist:
        """Render a podcast show as a playlist item (cover + title)."""
        owner_id = str(podcast.get("owner_id"))
        item_id = f"podcast:{owner_id}"
        name = self._clean_title(podcast.get("podcast_title")) or f"Подкаст {owner_id}"
        playlist = Playlist(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            owner=owner_id,
            provider_mappings=self._mapping(item_id),
        )
        best = None
        for thumb in podcast.get("thumbs", []):
            for size in thumb.get("sizes", []):
                if size.get("src") and (best is None or (size.get("width") or 0) > (best.get("width") or 0)):
                    best = size
        if best:
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=str(best["src"]),
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )
        return playlist

    async def _get_podcast(self, prov_playlist_id: str) -> dict:
        """Resolve a podcast:<owner_id> playlist reference to its raw show dict."""
        owner_id = prov_playlist_id.removeprefix("podcast:")
        podcast = self._podcast_cache.get(owner_id)
        if podcast is None:
            try:
                digest = await self._catalog_digest(
                    "https://vk.com/audio?section=podcasts_subscriptions&block=subscriptions"
                )
            except Exception:
                digest = {}
            for p in digest.get("podcasts", []):
                if p.get("owner_id"):
                    self._podcast_cache[str(p["owner_id"])] = p
            podcast = self._podcast_cache.get(owner_id)
        if podcast is None:
            raise MediaNotFoundError(f"VK podcast not found: {owner_id}")
        return podcast

    async def _browse_podcast_episodes(self, owner_id: str) -> list[Track]:
        """Fetch podcast episodes via the app's podcasts.getEpisodes method."""
        data = await self._request_data(
            VkApiRequestBuilder.build_from_base_request(
                method="get",
                url="podcasts.getEpisodes",
                params={"owner_id": owner_id, "count": 100},
            )
        )
        raw_items = [i for i in data.get("items", []) if isinstance(i, dict)]
        for raw in raw_items:
            if raw.get("url") and raw.get("owner_id") and raw.get("id"):
                # podcast episodes are not served by audio.getById on this
                # token surface: keep the fresh signed url for playback
                self._episode_cache[f"{raw['owner_id']}_{raw['id']}"] = (
                    str(raw["url"]),
                    time.time(),
                )
        return self._parse_raw_audios(raw_items)

    def _render_book_chapters(self, book: dict) -> list[Track]:
        """Render audiobook chapters as tracks and cache their stream urls."""
        items: list[Track] = []
        book_id = book.get("id")
        for index, chapter in enumerate(book.get("chapters", []), start=1):
            audio_file = chapter.get("audio_file") or {}
            url = audio_file.get("url")
            title = self._clean_title(chapter.get("title"))
            if not url or not title:
                continue
            item_id = f"abch:{book_id}:{chapter.get('id')}"
            duration = max(0, int(audio_file.get("duration") or 0))
            self._chapter_cache[item_id] = (
                str(url),
                duration,
                f"{index}. {title}",
                time.time(),
            )
            items.append(
                Track(
                    item_id=item_id,
                    provider=self.instance_id,
                    name=f"{index}. {title}",
                    duration=duration,
                    provider_mappings=self._mapping(item_id),
                )
            )
        return items

    async def _recent_tracks(self) -> list[Track]:
        """The «Недавно слушали» content (catalog.getAudio recent block)."""
        request = VkApiRequestBuilder.build_req_get_catalog_audio(
            url="https://vk.com/audios?section=recoms&block=recent",
            need_blocks=1,
        )
        data = await self._request_data(request)
        return self._parse_raw_audios(data.get("audios", []))

    async def _browse_user_playlists(self, offset: int, page_path: str) -> list[MediaItemType]:
        """One page of the user's playlists with a next-page folder."""
        user = await self._service.get_user_info_async()
        entries = await self._service.get_playlists_by_userid_async(
            user.userid, count=100, offset=offset
        )
        items: list[MediaItemType] = [
            self._parse_vk_collection(item, MediaType.PLAYLIST) for item in entries
        ]
        if len(entries) == 100:
            next_offset = offset + 100
            next_path = f"{page_path}/Ещё плейлисты"
            self._page_cache[next_path] = ("playlists", next_offset)
            items.append(
                self._catalog_folder(f"playlists:{next_offset}", "Ещё плейлисты", next_path)
            )
        return items

    async def _resume_cached_page(self, cached: tuple, current_path: str) -> list[MediaItemType]:
        """Resume a pagination folder registered earlier in _page_cache."""
        if cached[0] == "playlists":
            return await self._browse_user_playlists(cached[1], current_path)
        # ("block", block_id, start_from)
        data = await self._catalog_block_items(cached[1], cached[2])
        # catalog.getBlockItems answers with {block: {...ids...}, <pools>}
        blocks = [data["block"]] if isinstance(data.get("block"), dict) else []
        return self._render_catalog_blocks(blocks, data, current_path)

    async def _browse_parts(self, parts: list[str]) -> list[MediaItemType | ItemMapping | BrowseFolder]:
        """Walk a readable browse path («Моя музыка» / «Аудиокниги») down the catalog tree."""
        root = await self._catalog_digest()
        section = next(
            (
                s
                for s in root.get("catalog", {}).get("sections", [])
                if self._clean_title(s.get("title")) == parts[0]
            ),
            None,
        )
        if section is None or not section.get("url"):
            self.logger.debug("Unknown VK catalog path segment: %s", parts[0])
            return []

        digest = await self._catalog_digest(section["url"])
        index = 1
        while index < len(parts):
            current_path = "/".join(parts[: index + 1])
            leaf = parts[index]
            cached = self._page_cache.get(current_path)
            if cached is not None:
                return await self._resume_cached_page(cached, current_path)
            link = self._find_link(digest, leaf)
            if link is None:
                return []
            url = link.get("url") or ""
            if "section=recent" in url:
                return await self._recent_tracks()
            if "catalog=my_playlists" in url:
                offset = int(leaf.rsplit(":", 1)[1]) if ":" in leaf else 0
                return await self._browse_user_playlists(offset, current_path)
            if not self._is_catalog_link(url):
                return []
            digest = await self._catalog_digest(url)
            index += 1

        return self._render_catalog_digest(digest, section.get("url") or "", "/".join(parts))

    def _render_catalog_blocks(
        self, blocks: list[dict], digest: dict, current_path: str
    ) -> list[MediaItemType | BrowseFolder]:
        """Render catalog blocks (of a digest or a block-items page) into browse items."""
        items: list[MediaItemType | BrowseFolder] = []
        page_no = 0
        for block in blocks:
            data_type = block.get("data_type")
            if data_type == "links":
                links_pool = digest.get("links", [])
                for link_id in block.get("links_ids", []):
                    link = next((l for l in links_pool if l.get("id") == link_id), None)
                    if not link:
                        continue
                    url = link.get("url") or ""
                    name = self._clean_title(link.get("title")) or "Раздел"
                    if "section=recent" in url:
                        items.append(self._catalog_folder("recent", name, f"{current_path}/Недавно слушали"))
                    elif "catalog=my_playlists" in url:
                        items.append(self._catalog_folder("playlists:0", name, f"{current_path}/Плейлисты"))
                    elif url.startswith("https://vk.com/artist/"):
                        # artist page: rendered as an Artist item — MA opens the
                        # artist view (top tracks / albums) instead of a folder
                        best_image = max(
                            (i for i in link.get("image", []) if i.get("url")),
                            key=lambda i: i.get("width") or 0,
                            default=None,
                        )
                        items.append(
                            self._parse_artist(
                                name, best_image.get("url") if best_image else None
                            )
                        )
                    elif self._is_catalog_link(url):
                        items.append(self._catalog_folder(f"link:{link.get('id')}", name, f"{current_path}/{name}"))
            elif data_type == "music_playlists":
                playlists_pool = digest.get("playlists", [])
                for playlist_id in block.get("playlists_ids", []):
                    entry = next(
                        (
                            p
                            for p in playlists_pool
                            if f"{p.get('owner_id')}_{p.get('id')}" == str(playlist_id)
                        ),
                        None,
                    )
                    if entry is not None:
                        items.append(
                            self._parse_vk_collection(VKPlaylist.from_json(entry), MediaType.PLAYLIST)
                        )
            elif data_type == "music_audios":
                audios_pool = digest.get("audios", [])
                seen_ids: set[str] = set()
                for audio_id in block.get("audios_ids", []):
                    if audio_id in seen_ids:
                        continue
                    seen_ids.add(audio_id)
                    entry = next(
                        (
                            a
                            for a in audios_pool
                            if f"{a.get('owner_id')}_{a.get('id')}" == str(audio_id)
                        ),
                        None,
                    )
                    if entry is not None:
                        items.append(
                            self._parse_song(
                                VKSong.from_json(entry), self._thumb_from_raw(entry)
                            )
                        )
                if block.get("next_from"):
                    page_no += 1
                    seg = "Ещё треки" if page_no == 1 else f"Ещё треки {page_no}"
                    page_path = f"{current_path}/{seg}"
                    self._page_cache[page_path] = ("block", block.get("id"), block["next_from"])
                    items.append(
                        self._catalog_folder(
                            f"more:{block.get('id')}:{block['next_from']}", seg, page_path
                        )
                    )
            elif data_type == "audio_books":
                books_pool = digest.get("audio_books", [])
                for book_id in block.get("audio_book_ids", []):
                    entry = next(
                        (b for b in books_pool if str(b.get("id")) == str(book_id)), None
                    )
                    if entry is None or not entry.get("chapters"):
                        # books without resolvable chapters cannot be played
                        continue
                    self._book_cache[str(book_id)] = entry
                    items.append(self._parse_book_playlist(entry))
            elif data_type == "podcasts":
                podcasts_pool = digest.get("podcasts", [])
                for podcast_ref in block.get("podcast_items_ids", []):
                    entry = next(
                        (
                            p
                            for p in podcasts_pool
                            if f"{p.get('owner_id')}_{p.get('id')}" == str(podcast_ref)
                        ),
                        None,
                    )
                    if entry is None or not entry.get("owner_id"):
                        continue
                    self._podcast_cache[str(entry["owner_id"])] = entry
                    items.append(self._parse_podcast_playlist(entry))
        return items

    def _render_catalog_digest(
        self, digest: dict, requested_url: str, current_path: str
    ) -> list[MediaItemType | BrowseFolder]:
        """Render the resolved section of one catalog digest into browse items."""
        catalog = digest.get("catalog", digest)
        target = next(
            (
                section
                for section in catalog.get("sections", [])
                if section.get("id") == catalog.get("default_section")
            ),
            None,
        )
        if target is None:
            return []
        if (
            (target.get("url") or "") != requested_url
            and self._clean_title(target.get("title")) == "Главная"
        ):
            # catalog.getAudio silently returns the default digest for
            # urls it does not understand (catalog=..., vk.cc, popup=...)
            self.logger.debug(
                "VK catalog digest for %s fell back to default, skipping", requested_url
            )
            return []

        return self._render_catalog_blocks(target.get("blocks", []), digest, current_path)

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse the VK catalog tree: personal library, playlists, books, podcasts."""
        subpath = path.split("://", 1)[1] if "://" in path else path
        # Browse paths are hierarchical («Моя музыка»/«Аудиокниги»): the
        # frontend derives breadcrumbs and the parent level from the "/"-separated
        # segments, so each folder carries a readable `path`.
        parts = [p.strip() for p in subpath.split("/") if p.strip()]
        if not parts:
            # Root mirrors the VK app's own tabs (Главная / Обзор / Книги и
            # шоу / Моя музыка) from the url=None catalog digest.
            root = await self._catalog_digest()
            items: list[MediaItemType | ItemMapping | BrowseFolder] = []
            for section in root.get("catalog", {}).get("sections", []):
                name = self._clean_title(section.get("title"))
                if not name or not section.get("url"):
                    continue
                items.append(
                    self._catalog_folder(f"section:{name}", name, name)
                )
            return items
        return await self._browse_parts(parts)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve a fresh stream URL immediately before playback."""
        try:
            cached_episode = self._episode_cache.get(item_id)
            if (
                cached_episode is not None
                and time.time() - cached_episode[1] > self._STREAM_URL_TTL
            ):
                # signed url expired: re-fetch the show's episodes
                await self._browse_podcast_episodes(item_id.partition("_")[0])
                cached_episode = self._episode_cache.get(item_id)
            if cached_episode is not None:
                # podcast episode: audio.getById does not serve episodes on
                # this token surface; reuse the url from podcasts.getEpisodes.
                # Episode urls (/s/v1/ac/) 302-redirect to an HLS playlist.
                url = cached_episode[0]
                is_hls = "/s/v1/ac/" in url or "m3u8" in url
                content_type = ContentType.try_parse(url.split("?", 1)[0]) or (
                    ContentType.AAC if is_hls else ContentType.MP3
                )
                return StreamDetails(
                    provider=self.instance_id,
                    item_id=item_id,
                    media_type=MediaType.TRACK,
                    audio_format=AudioFormat(content_type=content_type),
                    stream_type=StreamType.HLS if is_hls else StreamType.HTTP,
                    path=url,
                    can_seek=is_hls,
                    allow_seek=is_hls,
                )
            if item_id.startswith("abch:"):
                # audiobook chapter: the signed stream url was cached when the
                # chapter list was browsed; refresh the book on a cache miss or
                # an expired signed url (e.g. a queue persisted before restart)
                cached = self._chapter_cache.get(item_id)
                if cached is None or time.time() - cached[3] > self._STREAM_URL_TTL:
                    book = await self._refresh_book(item_id.split(":")[1])
                    if book:
                        self._render_book_chapters(book)
                    cached = self._chapter_cache.get(item_id)
                if cached is None:
                    raise MediaNotFoundError(
                        f"VK audiobook chapter stream expired, re-open the book: {item_id}"
                    )
                url = cached[0]
                content_type = ContentType.try_parse(url.split("?", 1)[0]) or ContentType.MP3
                return StreamDetails(
                    provider=self.instance_id,
                    item_id=item_id,
                    media_type=MediaType.TRACK,
                    audio_format=AudioFormat(content_type=content_type),
                    stream_type=StreamType.HTTP,
                    path=url,
                    can_seek=True,
                    allow_seek=True,
                )
            owner, separator, track = item_id.partition("_")
            if not separator or not owner or not track:
                raise MediaNotFoundError(f"Invalid VK track id: {item_id}")
            try:
                songs = await self._service.get_songs_by_id_async([f"{owner}_{track}"])
            except VkApiException as err:
                if getattr(err, "error_code", None) in (5, 201):
                    raise LoginFailed("VK denied access to this audio; check account entitlement") from err
                raise
            if not songs or not songs[0].url:
                if owner.startswith("-"):
                    # community audio that fails audio.getById may be a podcast
                    # episode — self-heal via podcasts.getEpisodes once
                    await self._browse_podcast_episodes(owner)
                    episode_url = self._episode_cache.get(item_id)
                    if episode_url:
                        url = episode_url[0]
                        is_hls = "/s/v1/ac/" in url or "m3u8" in url
                        content_type = ContentType.try_parse(
                            url.split("?", 1)[0]
                        ) or (ContentType.AAC if is_hls else ContentType.MP3)
                        return StreamDetails(
                            provider=self.instance_id,
                            item_id=item_id,
                            media_type=MediaType.TRACK,
                            audio_format=AudioFormat(content_type=content_type),
                            stream_type=StreamType.HLS if is_hls else StreamType.HTTP,
                            path=url,
                            can_seek=is_hls,
                            allow_seek=is_hls,
                        )
                raise MediaNotFoundError(f"VK did not provide an accessible stream for {item_id}")
            url = songs[0].url
            content_type = ContentType.try_parse(url.split("?", 1)[0])
            self.logger.debug(
                "VK stream metadata resolved for %s (content_type=%s)",
                item_id,
                content_type,
            )
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                media_type=MediaType.TRACK,
                audio_format=AudioFormat(content_type=content_type),
                stream_type=StreamType.HTTP,
                path=url,
                can_seek=True,
                allow_seek=True,
            )
        except Exception as err:
            self.logger.exception(
                "VK playback stream resolution failed for %s (%s)",
                item_id,
                type(err).__name__,
            )
            raise
