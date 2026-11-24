"""Parsing of raw VK API objects into Music Assistant media items."""
from __future__ import annotations

import base64
import json

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    Track,
    UniqueList,
)

from .base import ProviderContract
from .vendor.vkpymusic.models import Song as VKSong


class MediaParsersMixin(ProviderContract):
    """VK object -> MA item converters."""

    def _parse_song(self, song, thumb: str | None = None) -> Track:
        """Convert VK audio metadata into a Music Assistant track."""
        item_id = f"{song.owner_id}_{song.track_id}"
        artist_items: list[Artist | ItemMapping] = []
        if song.artist:
            artist_items.append(
                ItemMapping(
                    item_id=f"artist:{song.artist.casefold()}",
                    provider=self.instance_id,
                    name=song.artist,
                    media_type=MediaType.ARTIST,
                )
            )
        track = Track(
            item_id=item_id,
            provider=self.instance_id,
            name=song.title or "Unknown title",
            artists=UniqueList(artist_items),
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
