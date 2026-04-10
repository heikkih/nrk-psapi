from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime  # noqa: TC003
from typing import Any

from mashumaro import field_options

from .catalog import Link
from .common import BaseDataClassORJSONMixin, Enabled, StrEnum


class FavouriteSourceType(StrEnum):
    CONSUMED = "consumed"
    MANUAL = "manual"


class FavouriteType(StrEnum):
    PROGRAM = "program"
    SERIES = "series"
    PODCAST = "podcast"


class FavouriteLevel(StrEnum):
    AUTO = "auto"
    CONSUMED_FAVOURITES = "consumed_favourites"
    MANUAL_FAVOURITES = "manual_favourites"
    FAVOURITES_WITH_PUSH_NOTIFICATIONS = "favourites_with_push_notifications"


class ProgressContentType(StrEnum):
    PROGRAMS = "programs"
    PODCASTEPISODE = "podcastepisode"


class UpNextContentType(StrEnum):
    PROGRAMS = "programs"
    PODCASTEPISODE = "podcastepisode"


class UpNextContext(StrEnum):
    SERIES = "series"
    NEW_EPISODES = "new_episodes"


class QueueContentType(StrEnum):
    PROGRAMS = "programs"
    PODCASTEPISODE = "podcastepisode"


class QueuePositionWhere(StrEnum):
    FIRST = "first"
    LAST = "last"
    AFTER = "after"


@dataclass
class ProgressReportInterval(BaseDataClassORJSONMixin):
    not_before: str = field(metadata=field_options(alias="notBefore"))
    interval: str


@dataclass
class UserProgressLinks(BaseDataClassORJSONMixin):
    self: Link
    programs: Link | None = None
    podcast_episode: Link | None = field(default=None, metadata=field_options(alias="podcastepisode"))
    delete_progress: Link | None = field(default=None, metadata=field_options(alias="deleteProgress"))


@dataclass
class UserProgressResponse(BaseDataClassORJSONMixin):
    id: str
    progress: str
    report_interval: ProgressReportInterval | None = field(
        default=None, metadata=field_options(alias="reportInterval")
    )
    _links: UserProgressLinks | None = None


@dataclass
class UpNextLinks(BaseDataClassORJSONMixin):
    self: Link
    podcastepisode: Link | None = None
    programs: Link | None = None
    progress: Link | None = None
    next: Link | None = None


@dataclass
class UpNextEmbedded(BaseDataClassORJSONMixin):
    progress: UserProgressResponse | None = None


@dataclass
class UpNextResponse(BaseDataClassORJSONMixin):
    content_id: str = field(metadata=field_options(alias="contentId"))
    up_next_source: str = field(metadata=field_options(alias="upNextSource"))
    up_next_content_type: str | None = field(default=None, metadata=field_options(alias="upNextContentType"))
    _links: UpNextLinks | None = None
    _embedded: UpNextEmbedded | None = None


@dataclass
class QueueStatus(BaseDataClassORJSONMixin):
    code: str
    message: str | None = None


@dataclass
class QueueElement(BaseDataClassORJSONMixin):
    id: str
    type: str | None = None
    _embedded: dict[str, Any] | None = None


@dataclass
class QueueLinks(BaseDataClassORJSONMixin):
    self: Link
    add: Link
    delete: Link


@dataclass
class QueueResponse(BaseDataClassORJSONMixin):
    queue: list[QueueElement]
    status: QueueStatus
    _links: QueueLinks | None = None


@dataclass
class ContinuationItem(BaseDataClassORJSONMixin):
    id: str
    type: str
    _links: dict[str, Any] | None = None
    _embedded: dict[str, Any] | None = None


@dataclass
class ContinuationsLinks(BaseDataClassORJSONMixin):
    self: Link
    next: Link | None = None


@dataclass
class ContinuationsResponse(BaseDataClassORJSONMixin):
    continuations: list[ContinuationItem]
    _links: ContinuationsLinks | None = None


@dataclass
class UserFavouritesLinks(BaseDataClassORJSONMixin):
    self: Link
    next: Link | None = None


@dataclass
class UserFavouriteLinks(BaseDataClassORJSONMixin):
    self: Link
    share: Link
    delete_favourite: Link | None = field(default=None, metadata=field_options(alias="deleteFavourite"))
    unmark_favourite: Link | None = field(default=None, metadata=field_options(alias="unmarkFavourite"))
    push_notifications: Link | None = field(default=None, metadata=field_options(alias="pushNotifications"))


@dataclass
class UserFavourites(BaseDataClassORJSONMixin):
    _links: list[UserFavouritesLinks]
    id: str
    favourite_content_type: str = field(metadata=field_options(alias="favouriteContentType"))
    favourite_source: FavouriteSourceType = field(metadata=field_options(alias="favouriteSource"))
    push_notifications: Enabled = field(metadata=field_options(alias="pushNotifications"))
    _embedded: str = field(metadata=field_options(alias="_embedded"))


@dataclass
class UserFavourite(BaseDataClassORJSONMixin):
    _links: UserFavouriteLinks
    push_notifications: Enabled = field(metadata=field_options(alias="pushNotifications"))


@dataclass
class UserFavouritesResponse(BaseDataClassORJSONMixin):
    _links: UserFavouritesLinks
    favourites: list[UserFavourite]


@dataclass
class UserFavouriteNewEpisodesCountResponse(BaseDataClassORJSONMixin):
    count: int
    since: datetime
