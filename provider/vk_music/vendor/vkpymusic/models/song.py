"""
This module contains the Song class.
"""

import re
from typing import Optional


class Song:
    """
    A class that represents a song.

    Attributes:
        title (str): The title of the song.
        artist (str): The artist of the song.
        duration (int): The duration of the song in seconds.
        track_id (str): The ID of the song.
        owner_id (str): The ID of the song's owner.
        url (str): The URL of the song.
        thumb (str): The URL of the song's cover image (from album.thumb,
            prefers photo_600, falls back to photo_300/photo_68).
        subtitle (str): The subtitle of the song (e.g. featured artists).
        is_explicit (bool): Whether the song is marked as explicit.
        main_artists (list): The main artists of the song (raw VK dicts
            with 'id', 'name' and other fields).
    """

    title: str
    artist: str
    duration: int
    track_id: str
    owner_id: str
    url: str
    thumb: str
    subtitle: str
    is_explicit: bool
    main_artists: list

    def __init__(
        self,
        title: str,
        artist: str,
        duration: int,
        track_id: str,
        owner_id: str,
        url: str = "",
        thumb: str = "",
        subtitle: str = "",
        is_explicit: bool = False,
        main_artists: Optional[list] = None,
    ) -> None:
        """
        Initializes a Song object.

        Args:
            title (str): The title of the song.
            artist (str): The artist of the song.
            duration (int): The duration of the song in seconds.
            track_id (str): The ID of the song.
            owner_id (str): The ID of the song's owner.
            url (str): The URL of the song.
            thumb (str): The URL of the song's cover image.
            subtitle (str): The subtitle of the song.
            is_explicit (bool): Whether the song is marked as explicit.
            main_artists (Optional[list]): The main artists of the song.
        """
        self.title = title
        self.artist = artist
        self.duration = duration
        self.track_id = track_id
        self.owner_id = owner_id
        self.url = url
        self.thumb = thumb
        self.subtitle = subtitle
        self.is_explicit = is_explicit
        self.main_artists = main_artists if main_artists is not None else []

    def __str__(self):
        return f"{self.title} - {self.artist}"

    def to_dict(self) -> dict:
        """
        Converts the song to a dictionary.

        Returns:
            dict: The song as a dictionary.
        """
        return self.__dict__

    def to_safe(self):
        """
        Removes all non-alphanumeric characters from the song's title and artist.
        """

        def safe_format(string):
            safe_string = re.sub(r"[^A-zА-я0-9+\s]", "", string)
            return safe_string

        self.title = safe_format(self.title)
        self.artist = safe_format(self.artist)

    @classmethod
    def from_json(cls, item) -> "Song":
        """
        Creates a Song object from a JSON object.

        Args:
            item (dict): A JSON object.

        Returns:
            Song: A Song object.
        """
        title = str(item["title"])
        artist = str(item["artist"])
        duration = int(item["duration"])
        track_id = str(item["id"])
        owner_id = str(item["owner_id"])
        url = str(item["url"])

        thumb = ""
        album = item.get("album")
        if isinstance(album, dict) and isinstance(album.get("thumb"), dict):
            thumb_obj = album["thumb"]
            for photo_key in ("photo_600", "photo_300", "photo_68"):
                photo_url = thumb_obj.get(photo_key)
                if photo_url:
                    thumb = str(photo_url)
                    break

        subtitle = str(item.get("subtitle", ""))
        is_explicit = bool(item.get("is_explicit", False))
        main_artists = list(item.get("main_artists", []))

        song = cls(
            title,
            artist,
            duration,
            track_id,
            owner_id,
            url,
            thumb,
            subtitle,
            is_explicit,
            main_artists,
        )
        return song
