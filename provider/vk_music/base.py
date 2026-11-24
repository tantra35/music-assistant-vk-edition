"""Typing base for the provider mixin modules.

Under TYPE_CHECKING the mixins derive from the real (server-side)
``ProviderBase`` so that ``self.instance_id`` / ``self.domain`` resolve for
type checkers. At runtime they derive from an EMPTY stub: the mixins are
placed BEFORE MusicProvider in the final MRO, so any member defined here
would shadow the real ProviderBase properties — the stub must add nothing.

Every other declaration below is a member that does NOT exist on
ProviderBase: it is provided by VKMusicProvider itself (core methods in
__init__.py) or by a sibling mixin; the contract sits LAST in the MRO, so
its stubs never win at runtime.
"""
from __future__ import annotations

from logging import Logger
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    Album,
    Artist,
    Playlist,
    ProviderMapping,
    Track,
)

if TYPE_CHECKING:
    from music_assistant.models.provider import ProviderBase as _ProviderBase
    from .vendor.vkpymusic import Service
else:
    class _ProviderBase:  # runtime stub: adds no members
        pass


class ProviderContract(_ProviderBase):
    """Attributes and plumbing the mixins may rely on."""

    logger: Logger
    _service: Service
    _book_cache: dict[str, dict]
    _podcast_cache: dict[str, dict]
    _episode_cache: dict[str, tuple[str, float, str]]
    _chapter_cache: dict[str, tuple[str, int, str, float]]

    # core plumbing (defined in __init__.py)
    def _mapping(self, item_id: str) -> set[ProviderMapping]:
        ...

    async def _request_data(self, request) -> dict:
        ...

    def _clean_title(self, title: str | None) -> str:
        ...

    # media parsers (parsers.py)
    def _parse_song(self, song, thumb: str | None = None) -> Track:
        ...

    def _parse_raw_audios(self, items: list) -> list[Track]:
        ...

    def _parse_artist(self, name: str, image: str | None = None) -> Artist:
        ...

    def _parse_vk_collection(self, entry, media_type: MediaType) -> Album | Playlist:
        ...

    def _best_image_url(self, images: list) -> str | None:
        ...

    def _thumb_from_raw(self, raw: dict) -> str | None:
        ...

    # collection id helpers (parsers.py)
    @staticmethod
    def _encode_collection_id(entry, media_type: MediaType) -> str:
        ...

    @staticmethod
    def _decode_collection_id(item_id: str) -> dict[str, str]:
        ...

    def _apply_collection_metadata(self, item: Album | Playlist, payload: dict[str, str]) -> None:
        ...

    # books & podcasts (books_podcasts.py)
    async def _refresh_book(self, book_id: str) -> dict | None:
        ...

    async def _get_book(self, prov_playlist_id: str) -> dict:
        ...

    def _parse_book_playlist(self, book: dict) -> Playlist:
        ...

    async def _get_podcast(self, prov_playlist_id: str) -> dict:
        ...

    def _parse_podcast_playlist(self, podcast: dict) -> Playlist:
        ...

    async def _browse_podcast_episodes(self, owner_id: str) -> list[Track]:
        ...

    def _render_book_chapters(self, book: dict) -> list[Track]:
        ...

    # catalog (catalog.py)
    async def _catalog_digest(self, url: str | None = None) -> dict:
        ...
