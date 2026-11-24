"""VK search: tracks, artists, albums and playlists."""
from __future__ import annotations

from typing import cast

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Artist, Playlist, SearchResults

from .base import ProviderContract
from .vendor.vkpymusic.models import Playlist as VKPlaylist
from .vendor.vkpymusic.vk_api import VkApiException
from .vendor.vkpymusic.vk_api.vk_api_request_builder import VkApiRequestBuilder


class SearchMixin(ProviderContract):
    """audio.search* based discovery."""

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
                    results.albums = cast("list[Album]", collections)
                else:
                    results.playlists = cast("list[Playlist]", collections)
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
