"""Mirror roles from one guild to another with hierarchy parity.

Roles are created lowest-position-first so the resulting hierarchy matches
the source guild. ``@everyone`` and managed (integration/bot) roles are
skipped; the bot's own auto-created role should be skipped by name via
``skip_names``.

Returns a list of {"old_id", "new_id", "name"} dicts for channel permission
overwrite remapping.
"""

import time


def mirror_roles(api, source_guild_id, target_guild_id, skip_names=()):
    old_roles = api.get("/guilds/%s/roles" % source_guild_id)
    if not old_roles:
        print("Could not fetch roles from source guild.")
        return []

    candidates = []
    for role in old_roles:
        if role["name"] == "@everyone":
            continue
        if role.get("managed", False):
            continue
        if role["name"] in skip_names:
            continue
        candidates.append(role)

    # Create lowest first so positions stack up correctly.
    candidates.sort(key=lambda r: r.get("position", 0))

    mapping = []
    print("Mirroring %d roles..." % len(candidates))
    for role in candidates:
        result = api.post(
            "/guilds/%s/roles" % target_guild_id,
            {
                "name": role["name"],
                "permissions": role["permissions"],
                "color": role["color"],
                "hoist": role["hoist"],
                "mentionable": role["mentionable"],
            },
        )
        if result and result.get("id"):
            print("  Created role: %s" % role["name"])
            mapping.append(
                {"old_id": role["id"], "new_id": result["id"], "name": role["name"]}
            )
        else:
            print("  FAILED to create role: %s" % role["name"])
        time.sleep(0.5)
    return mapping


def remap_overwrites(overwrites, role_mapping):
    """Rewrite permission overwrite role IDs using the role mapping.

    Overwrites referencing roles that were not mirrored are dropped (with the
    member-specific ``type: 1`` overwrites kept as-is since user IDs are
    guild-independent).
    """
    id_map = {entry["old_id"]: entry["new_id"] for entry in role_mapping}
    remapped = []
    for ow in overwrites or []:
        if ow.get("type") == 0:  # role overwrite
            new_id = id_map.get(ow["id"])
            if not new_id:
                continue
            ow = dict(ow)
            ow["id"] = new_id
        remapped.append(ow)
    return remapped
