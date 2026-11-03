"""Fetch message history and re-post it through a channel webhook.

Each original message becomes one webhook execution with the original
author's display name and avatar, so the migrated channel reads like the
original. ``allowed_mentions`` is locked to ``parse: []`` so a migration
never pings anyone.

Honest limits (also documented in the README):
- Discord stamps the *creation* time at post time; original timestamps
  cannot be preserved. Posting sequentially preserves order. The optional
  ``timestamp_tz`` mode compensates by prepending an ISO 8601 header line
  with the original timestamp and author name to each copied message.
- Replies (message_reference) cannot be recreated cross-guild; they are
  posted as plain messages.
- Reactions are dropped.
"""

import datetime
from zoneinfo import ZoneInfo

from . import webhooks

MAX_CONTENT_LENGTH = 2000
MAX_EMBEDS = 10


def split_content(content):
    """Split a message into <=2000 char chunks on line boundaries."""
    if not content or len(content) <= MAX_CONTENT_LENGTH:
        return [content] if content else []
    chunks = []
    current = []
    current_len = 0
    for line in content.split("\n"):
        # +1 for the newline we will re-add
        if current_len + len(line) + 1 > MAX_CONTENT_LENGTH and current:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        # A single pathological line longer than the limit gets hard-cut.
        while len(line) > MAX_CONTENT_LENGTH:
            chunks.append(line[:MAX_CONTENT_LENGTH])
            line = line[MAX_CONTENT_LENGTH:]
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def author_name(message):
    """Display name for a message: member nick, else global name, else username."""
    author = message.get("author", {})
    member = message.get("member", {})
    return member.get("nick") or author.get("global_name") or author.get("username") or "Unknown"


def timestamp_header(message, tz_name="UTC"):
    """ISO 8601 header line carrying the message's original time and author.

    Example: ``[2026-11-02T20:22:41-06:00] Christina`` for tz
    ``America/Chicago``. Returns None when the message has no timestamp.
    """
    raw = message.get("timestamp")
    if not raw:
        return None
    moment = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    local = moment.astimezone(ZoneInfo(tz_name))
    return "[%s] %s" % (local.isoformat(timespec="seconds"), author_name(message))


def build_payload(message, timestamp_tz=None):
    """Convert a Discord message object into a webhook execution payload.

    With ``timestamp_tz`` (an IANA timezone name), the returned content is
    prefixed with an ISO 8601 header line showing the original timestamp
    and author name, so the copy keeps its true date even though Discord
    stamps the copy with the migration time.
    """
    author = message.get("author", {})
    username = author_name(message)
    avatar_url = None
    avatar = author.get("avatar")
    if avatar and author.get("id"):
        avatar_url = "https://cdn.discordapp.com/avatars/%s/%s.png" % (
            author["id"],
            avatar,
        )

    payload = {
        "username": username[:80],  # webhook username limit
        "allowed_mentions": {"parse": []},
    }
    if avatar_url:
        payload["avatar_url"] = avatar_url

    embeds = message.get("embeds") or []
    if len(embeds) > MAX_EMBEDS:
        embeds = embeds[:MAX_EMBEDS]
    if embeds:
        payload["embeds"] = embeds

    if message.get("message_reference"):
        # Cross-guild reply references are invalid; the message posts standalone.
        pass

    content = message.get("content") or ""
    if timestamp_tz is not None:
        header = timestamp_header(message, timestamp_tz)
        if header:
            content = header + "\n" + content if content else header

    return payload, content


def migrate_channel(api, source_channel_id, webhook, checkpoint, on_progress=None,
                    min_interval=None, timestamp_tz=None):
    """Migrate every message from a source channel through ``webhook``.

    ``checkpoint`` is a mutable dict persisted by the caller: it maps
    source channel IDs to the last migrated source message ID, enabling
    resume. ``min_interval`` overrides the default spacing between webhook
    posts (rate-limit safety). ``timestamp_tz`` (an IANA timezone name)
    prepends an ISO 8601 original-timestamp header to each copied message.
    Returns (migrated_count, skipped_count).
    """
    from .api import WEBHOOK_POST_FLOOR_SECONDS

    if min_interval is None:
        min_interval = WEBHOOK_POST_FLOOR_SECONDS
    migrated = 0
    skipped = 0
    last_id = checkpoint.get(source_channel_id)

    for message in api.paginate_messages(source_channel_id):
        if last_id and int(message["id"]) <= int(last_id):
            continue  # already migrated in a previous run

        if message.get("type", 0) not in (0, 19):
            # Only normal messages and thread-starter-adjacent replies; skip
            # pins, joins, boosts, and other system messages.
            skipped += 1
            checkpoint[source_channel_id] = message["id"]
            continue

        payload, content = build_payload(message, timestamp_tz=timestamp_tz)

        # Re-upload attachments (skip ones that are too large to carry).
        files = []
        for attachment in message.get("attachments") or []:
            downloaded = webhooks.download_attachment(attachment["url"])
            if downloaded:
                files.append(downloaded)
            else:
                skipped += 1
                print("    Skipped oversized/unreachable attachment in msg %s" % message["id"])

        chunks = split_content(content)
        if not chunks and not files and not payload.get("embeds"):
            checkpoint[source_channel_id] = message["id"]
            continue  # empty message (e.g. sticker-only); nothing to post

        ok = True
        for index, chunk in enumerate(chunks or [None]):
            part = dict(payload)
            if chunk is not None:
                part["content"] = chunk
            # Only the first chunk carries embeds/attachments to avoid dupes.
            part_files = files if index == 0 else []
            if index > 0:
                part.pop("embeds", None)
            result = webhooks.execute(webhook, part, part_files, min_interval=min_interval)
            if not result:
                ok = False
                break
        if ok:
            migrated += 1
        else:
            skipped += 1
        checkpoint[source_channel_id] = message["id"]
        if on_progress:
            on_progress(migrated, skipped)

    return migrated, skipped
