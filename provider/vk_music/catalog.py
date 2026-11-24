"""VK catalog browse: digest tree navigation and «Ещё треки» pagination."""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import BrowseFolder, Track

from .base import ProviderContract
from .constants import (
    _CATALOG_LINK_PREFIX,
    _MAX_AGGREGATED_PLAYLISTS,
    _MAX_AGGREGATED_TRACKS,
    _MORE_TRACKS_RE,
)
from .vendor.vkpymusic.models import Playlist as VKPlaylist
from .vendor.vkpymusic.models import Song as VKSong
from .vendor.vkpymusic.vk_api.vk_api_extensions import make_request_async
from .vendor.vkpymusic.vk_api.vk_api_request_builder import VkApiRequestBuilder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import ItemMapping, MediaItemType


class CatalogBrowseMixin(ProviderContract):
    """catalog.getAudio / catalog.getBlockItems tree walker."""

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

    def _catalog_folder(self, item_id: str, name: str, path: str) -> BrowseFolder:
        """Build a browse folder; `path` is the FULL navigation route
        (``instance_id://segment/segment``) — the frontend browses it verbatim,
        and the websocket handler splits on ``://``."""
        return BrowseFolder(
            item_id=item_id,
            provider=self.instance_id,
            name=name,
            path=f"{self.instance_id}://{path}",
        )

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

    async def _recent_tracks(self) -> list[Track]:
        """The «Недавно слушали» content (catalog.getAudio recent block)."""
        request = VkApiRequestBuilder.build_req_get_catalog_audio(
            url="https://vk.com/audios?section=recoms&block=recent",
            need_blocks=1,
        )
        data = await self._request_data(request)
        return self._parse_raw_audios(data.get("audios", []))

    async def _browse_user_playlists(self, offset: int = 0) -> Sequence[MediaItemType]:
        """The user's playlists, aggregated across offset pages.

        MA's browse has no server-side paging (the frontend fetches a folder
        in one request), so «Ещё плейлисты» continuation folders would just
        be nested navigation — walk the offsets here instead.
        """
        user = await self._service.get_user_info_async()
        items: list[MediaItemType] = []
        fetched = 0
        while True:
            entries = await self._service.get_playlists_by_userid_async(
                user.userid, count=100, offset=offset + fetched
            )
            items.extend(
                self._parse_vk_collection(item, MediaType.PLAYLIST) for item in entries
            )
            if len(entries) < 100 or len(items) >= _MAX_AGGREGATED_PLAYLISTS:
                break
            fetched += 100
        return items

    async def _browse_parts(
        self, parts: list[str]
    ) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
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
            leaf = parts[index]
            more_match = _MORE_TRACKS_RE.match(leaf)
            if more_match is not None:
                skip = int(more_match.group(1) or 0)
                block_no = int(more_match.group(2) or 1)
                return await self._resume_track_page(
                    digest, section.get("url") or "", skip, block_no, "/".join(parts)
                )
            link = self._find_link(digest, leaf)
            if link is None:
                return []
            url = link.get("url") or ""
            if "section=recent" in url:
                return await self._recent_tracks()
            if "catalog=my_playlists" in url:
                offset = int(leaf.rsplit(":", 1)[1]) if ":" in leaf else 0
                return await self._browse_user_playlists(offset)
            if not self._is_catalog_link(url):
                return []
            digest = await self._catalog_digest(url)
            index += 1

        return await self._render_catalog_digest(digest, section.get("url") or "", "/".join(parts))

    def _resolve_target_section(self, digest: dict, requested_url: str) -> dict | None:
        """Resolve the target section of a digest (with the fallback check)."""
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
            return None
        if (
            (target.get("url") or "") != requested_url
            and self._clean_title(target.get("title")) == "Главная"
        ):
            # catalog.getAudio silently returns the default digest for
            # urls it does not understand (catalog=..., vk.cc, popup=...)
            self.logger.debug(
                "VK catalog digest for %s fell back to default, skipping", requested_url
            )
            return None
        return target

    async def _walk_block_audios(
        self, block: dict, digest: dict, skip: int, limit: int, seen_ids: set
    ) -> tuple[list, int, bool]:
        """Walk a paginated music_audios block chain.

        Skips the first `skip` audios of the chain (stateless resume position)
        and collects at most `limit` tracks. Returns (tracks, chain_pos,
        has_more): chain_pos counts ALL ids of this chain from its start
        (regardless of the cross-block seen_ids dedupe), so it can be reused
        verbatim as the next skip. A VK error mid-chain returns the partial
        listing (has_more=False) instead of failing the whole browse.
        """
        tracks = []
        chain_pos = 0
        block_id = block.get("id")
        hop_block, hop_pool = block, digest.get("audios", [])
        has_more = False
        while True:
            for audio_id in hop_block.get("audios_ids", []):
                chain_pos += 1
                if audio_id in seen_ids:
                    continue
                seen_ids.add(audio_id)
                if chain_pos > skip and len(tracks) < limit:
                    entry = next(
                        (
                            a
                            for a in hop_pool
                            if f"{a.get('owner_id')}_{a.get('id')}" == str(audio_id)
                        ),
                        None,
                    )
                    if entry is not None:
                        tracks.append(
                            self._parse_song(
                                VKSong.from_json(entry), self._thumb_from_raw(entry)
                            )
                        )
            if len(tracks) >= limit:
                has_more = bool(block_id and hop_block.get("next_from"))
                break
            if not (block_id and hop_block.get("next_from")):
                break
            try:
                page = await self._catalog_block_items(block_id, hop_block["next_from"])
            except Exception as err:
                self.logger.warning(
                    "VK block pagination aborted after %d audios: %s", chain_pos, err
                )
                break
            raw_block = page.get("block")
            hop_block = raw_block if isinstance(raw_block, dict) else {}
            hop_pool = page.get("audios", [])
        return tracks, chain_pos, has_more

    async def _resume_track_page(
        self, digest: dict, requested_url: str, skip: int, block_no: int, current_path: str
    ) -> Sequence[MediaItemType | BrowseFolder]:
        """Show the next chunk of a paginated audios block («Ещё треки <consumed>»)."""
        target = self._resolve_target_section(digest, requested_url)
        if target is None:
            return []
        items: list[MediaItemType | BrowseFolder] = []
        seen_ids: set[str] = set()
        paginated_no = 0
        for block in target.get("blocks", []):
            if block.get("data_type") != "music_audios" or not block.get("next_from"):
                continue
            paginated_no += 1
            if paginated_no != block_no:
                continue
            tracks, chain_pos, has_more = await self._walk_block_audios(
                block, digest, skip=skip, limit=_MAX_AGGREGATED_TRACKS, seen_ids=seen_ids
            )
            items.extend(tracks)
            if has_more:
                seg = f"Ещё треки {chain_pos}" if block_no == 1 else f"Ещё треки {chain_pos}-{block_no}"
                items.append(self._catalog_folder(f"more:{seg}", seg, f"{current_path}/{seg}"))
            break
        return items

    async def _render_catalog_blocks(
        self, blocks: list[dict], digest: dict, current_path: str
    ) -> Sequence[MediaItemType | BrowseFolder]:
        """Render catalog blocks (of a digest or a block-items page) into browse items."""
        items: list[MediaItemType | BrowseFolder] = []
        continuation: list[BrowseFolder] = []
        seen_ids: set[str] = set()
        budget = _MAX_AGGREGATED_TRACKS
        paginated_no = 0
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
                is_paginated = bool(block.get("next_from"))
                if is_paginated:
                    paginated_no += 1
                if budget > 0:
                    tracks, chain_pos, has_more = await self._walk_block_audios(
                        block, digest, skip=0, limit=budget, seen_ids=seen_ids
                    )
                    budget -= len(tracks)
                    items.extend(tracks)
                    if has_more:
                        seg = (
                            f"Ещё треки {chain_pos}"
                            if paginated_no == 1
                            else f"Ещё треки {chain_pos}-{paginated_no}"
                        )
                        continuation.append(
                            self._catalog_folder(f"more:{seg}", seg, f"{current_path}/{seg}")
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
        # continuation folders always go to the very end of the listing
        items.extend(continuation)
        return items

    async def _render_catalog_digest(
        self, digest: dict, requested_url: str, current_path: str
    ) -> Sequence[MediaItemType | BrowseFolder]:
        """Render the resolved section of one catalog digest into browse items."""
        target = self._resolve_target_section(digest, requested_url)
        if target is None:
            return []
        return await self._render_catalog_blocks(target.get("blocks", []), digest, current_path)

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
