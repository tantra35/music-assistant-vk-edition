"""MA media API surface: tracks, albums, playlists, artists."""
from __future__ import annotations

from typing import cast

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, Playlist, Track

from .base import ProviderContract
from .vendor.vkpymusic.vk_api.vk_api_request_builder import VkApiRequestBuilder


class MediaApiMixin(ProviderContract):
    """get_track / get_album / get_playlist and friends."""

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
        if cached_episode is None:
            # a queue persisted before a provider restart: rebuild the episode
            # cache from podcasts.getEpisodes
            owner_id = prov_track_id.partition("_")[0]
            if owner_id.startswith("-"):
                await self._browse_podcast_episodes(owner_id)
                cached_episode = self._episode_cache.get(prov_track_id)
        if cached_episode is not None:
            # podcast episode: audio.getById does not serve episodes on this
            # token surface — rebuild metadata from the cached stream url
            url, _ts, ep_title = cached_episode
            return Track(
                item_id=prov_track_id,
                provider=self.instance_id,
                name=ep_title or prov_track_id,
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
        self.logger.debug("get_playlist_tracks %s page=%s entering", prov_playlist_id, page)
        if prov_playlist_id.startswith("book:"):
            # audiobook: chapters arrive whole (page 0 only)
            if page:
                return []
            book = await self._get_book(prov_playlist_id)
            return self._render_book_chapters(book)
        if prov_playlist_id.startswith("podcast:"):
            # podcast episodes via the app's podcasts.getEpisodes
            if page:
                return []
            return await self._browse_podcast_episodes(
                prov_playlist_id.removeprefix("podcast:")
            )
        return await self._fetch_collection_tracks(prov_playlist_id, offset=page * 100)

    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Search VK albums using the artist name."""
        name = prov_artist_id.removeprefix("artist:")
        entries = await self._service.search_albums_by_text_async(name, count=30)
        return cast(
            "list[Album]",
            [self._parse_vk_collection(item, MediaType.ALBUM) for item in entries],
        )

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
