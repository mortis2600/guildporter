"""Create categories and channels in the target guild from a mapping.

The mapping config lists source channels (by name or ID) and their target
names. Categories are created first, then channels are placed inside them,
carrying over topic, nsfw flag, slowmode, and permission overwrites
(remapped through the role mapping).
"""

import time

# Channel types we know how to recreate. 0 = text, 2 = voice, 4 = category,
# 5 = announcement/news.
SUPPORTED_TYPES = {0: 0, 2: 2, 4: 4, 5: 5}


def find_channel(api, guild_id, ref):
    """Resolve a channel reference (ID or name) to a channel object."""
    if ref.isdigit():
        return api.get("/channels/%s" % ref)
    channels = api.get("/guilds/%s/channels" % guild_id)
    if not channels:
        return None
    for channel in channels:
        if channel.get("name") == ref and channel.get("type") in (0, 2, 5):
            return channel
    return None


def build_channels(api, target_guild_id, channel_entries, role_mapping, dry_run=False):
    """Create the target channels. Returns {source_id: target_id}."""
    channel_map = {}
    category_cache = {}

    def get_category(name):
        if name in category_cache:
            return category_cache[name]
        channels = api.get("/guilds/%s/channels" % target_guild_id) or []
        for channel in channels:
            if channel.get("type") == 4 and channel.get("name") == name:
                category_cache[name] = channel["id"]
                return channel["id"]
        if dry_run:
            print('  [dry-run] would create category "%s"' % name)
            return None
        created = api.post(
            "/guilds/%s/channels" % target_guild_id,
            {"name": name, "type": 4},
        )
        if created and created.get("id"):
            print('  Created category "%s"' % name)
            category_cache[name] = created["id"]
            return created["id"]
        print('  FAILED to create category "%s"' % name)
        return None

    for entry in channel_entries:
        source = entry.get("source_channel")
        target_name = entry.get("target", source.get("name") if isinstance(source, dict) else source)
        if source.get("type") not in SUPPORTED_TYPES:
            print('  Skipping "%s": channel type %s (threads/forums) not supported'
                  % (source.get("name"), source.get("type")))
            continue
        category = entry.get("category")
        parent_id = get_category(category) if category else None

        payload = {
            "name": target_name,
            "type": SUPPORTED_TYPES[source["type"]],
        }
        if parent_id:
            payload["parent_id"] = parent_id
        if source.get("topic"):
            payload["topic"] = source["topic"]
        if source.get("nsfw"):
            payload["nsfw"] = True
        if source.get("rate_limit_per_user"):
            payload["rate_limit_per_user"] = source["rate_limit_per_user"]

        from .roles import remap_overwrites

        overwrites = remap_overwrites(source.get("permission_overwrites"), role_mapping)
        if overwrites:
            payload["permission_overwrites"] = overwrites

        if dry_run:
            print('  [dry-run] would create channel "%s"' % target_name)
            continue
        created = api.post("/guilds/%s/channels" % target_guild_id, payload)
        if created and created.get("id"):
            print('  Created channel "%s"' % target_name)
            channel_map[source["id"]] = created["id"]
        else:
            print('  FAILED to create channel "%s"' % target_name)
        time.sleep(0.5)

    return channel_map
