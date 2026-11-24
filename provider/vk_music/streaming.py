"""Stream URL resolution immediately before playback."""
from __future__ import annotations

import time

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import LoginFailed, MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .base import ProviderContract
from .vendor.vkpymusic.vk_api import VkApiException


class StreamingMixin(ProviderContract):
    """get_stream_details with cache self-healing."""

    _STREAM_URL_TTL = 6 * 3600  # VK signed urls live well beyond this

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
