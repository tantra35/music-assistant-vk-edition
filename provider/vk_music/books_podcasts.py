"""Audiobooks and podcasts: chapters from digests, podcasts.getEpisodes."""
from __future__ import annotations

import time

from music_assistant_models.enums import ImageType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import MediaItemImage, Playlist, Track, UniqueList

from .base import ProviderContract
from .vendor.vkpymusic.vk_api import VkApiException
from .vendor.vkpymusic.vk_api.vk_api_request_builder import VkApiRequestBuilder


class BooksPodcastsMixin(ProviderContract):
    """Book chapter and podcast episode resolution with stream-url caches."""

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
        """Fetch podcast episodes via the app's podcasts.getEpisodes method.

        Best-effort self-heal: any VK error (e.g. 2301 «Podcasts is not
        available for this owner» for plain community audios) means the owner
        simply has no episodes — return empty instead of failing the caller.
        """
        try:
            data = await self._request_data(
                VkApiRequestBuilder.build_from_base_request(
                    method="get",
                    url="podcasts.getEpisodes",
                    params={"owner_id": owner_id, "count": 100},
                )
            )
        except VkApiException as err:
            self.logger.debug(
                "podcasts.getEpisodes failed for %s (%s) — treating as non-podcast",
                owner_id,
                getattr(err, "error_code", err),
            )
            return []
        raw_items = [i for i in data.get("items", []) if isinstance(i, dict)]
        for raw in raw_items:
            if raw.get("url") and raw.get("owner_id") and raw.get("id"):
                # podcast episodes are not served by audio.getById on this
                # token surface: keep the fresh signed url for playback
                self._episode_cache[f"{raw['owner_id']}_{raw['id']}"] = (
                    str(raw["url"]),
                    time.time(),
                    self._clean_title(raw.get("title")),
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
