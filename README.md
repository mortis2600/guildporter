# guildporter

Migrate a Discord server's message history to a new server, preserving the
original authors. Each message is re-posted through a channel webhook with
the original author's display name and avatar, so the new channel reads like
the original.

Zero dependencies. Pure Python standard library.

## What it does

- Mirrors roles (name, color, permissions, hoist, mentionable) lowest-first
  so the hierarchy matches the source server
- Creates categories and channels in the target server, carrying over topic,
  NSFW flag, slowmode, and permission overwrites (remapped to the new roles)
- Copies message history oldest-first through per-channel webhooks, with the
  original author's name and avatar on every message
- Re-uploads attachments and passes embeds through
- Checkpoint file per run: interrupt it, re-run it, and it resumes where it
  stopped instead of starting over
- `--dry-run` prints the whole plan before touching anything

## What it can't do (read this first)

This is a faithful *look*, not a forensic copy. Discord's API imposes real
limits on any migration tool, and guildporter is honest about them:

- **Timestamps are not preserved.** Discord stamps creation time when a
  message is posted. Messages are posted oldest-first, so order is correct,
  but every message will show today's date.
- **Replies can't be recreated.** Cross-guild reply references are invalid,
  so replies post as standalone messages.
- **Reactions are dropped.** There is no API to re-apply someone else's
  reactions.
- **Webhooks are rate-limited** (~30 posts/minute per webhook). Large
  channels take a while; the tool paces itself and honors 429s.
- **Mentions never ping.** `allowed_mentions` is locked to `parse: []` so a
  migration can't @everyone anyone by accident.

**Recommended practice:** archive the source server's channels (move them to
an ARCHIVE category) rather than deleting them. The original history,
timestamps, and reactions stay intact there as the record of truth.

## Requirements

A Discord bot token with these permissions **in both servers**:

- Manage Webhooks (create the masquerade webhooks)
- Read Message History (fetch source messages)
- Manage Channels + Manage Roles (only if migrating structure)

And the **Message Content privileged intent** enabled in the Discord
developer portal (Bot tab), otherwise message content comes back empty.

Keep the token in an environment variable, never in the config file:

```bash
export DISCORD_BOT_TOKEN="your-bot-token"
```

## Install

```bash
pipx install guildporter
# or
pip install guildporter
```

## Quickstart

1. Copy `examples/mapping.json` and fill in your guild IDs:

```json
{
  "source_guild": "1234567890",
  "target_guild": "0987654321",
  "migrate_roles": true,
  "migrate_channels": true,
  "channels": [
    { "source": "general", "target": "general", "category": "Community" },
    { "source": "announcements", "target": "announcements", "category": "Community" }
  ]
}
```

`source` accepts a channel name or a channel ID. `target` is the name to
create in the new server. `category` is optional.

2. Preview the plan:

```bash
guild-migrate --config mapping.json --dry-run
```

3. Migrate:

```bash
guild-migrate --config mapping.json
```

4. Optional: remove the GuildPorter webhooks from the target channels when
done:

```bash
guild-migrate --config mapping.json --cleanup-webhooks
```

## Config reference

| Key | Required | Default | Notes |
|---|---|---|---|
| `source_guild` | yes | - | Guild ID to read from |
| `target_guild` | yes | - | Guild ID to write to |
| `channels` | yes | - | List of channel mappings |
| `channels[].source` | yes | - | Channel name or ID in the source guild |
| `channels[].target` | no | source name | Channel name to create in the target guild |
| `channels[].category` | no | none | Category to place the new channel in (created if missing) |
| `migrate_roles` | no | `true` | Mirror roles with hierarchy parity |
| `migrate_channels` | no | `true` | Create channels; set `false` and use target channel IDs to post into existing channels |

## Rate limiting

guildporter is deliberately conservative, because a migration that trips
Discord's abuse detection helps nobody:

- Webhook posts are spaced 2.5s apart (~24/min, under the ~30/min limit);
  tune with `--post-delay SECONDS`
- `X-RateLimit-Remaining` / `X-RateLimit-Reset-After` response headers are
  honored proactively: when a bucket hits zero, the tool sleeps until reset
  instead of firing into a guaranteed 429
- 429 responses are retried using Discord's `retry_after` plus a safety
  margin; 5xx and network errors use exponential backoff (capped at 60s)
- History fetches pause 0.5s between pages; role/channel creation pauses
  0.5s between calls

## CLI options

```
guild-migrate --config mapping.json [--token TOKEN] [--vault] [--dry-run]
              [--checkpoint FILE] [--cleanup-webhooks]
              [--skip-bot-role NAME] [--post-delay SECONDS]
```

- `--token` / `DISCORD_BOT_TOKEN`: raw bot token (the `Bot ` scheme prefix
  is added automatically if missing).
- `--vault`: read the token from the Secure Vault instead (only works on
  the maintainer's VM; the vault secret must already include the `Bot `
  prefix, since it is swapped into the request verbatim).

- `--checkpoint`: where resume state is stored (default
  `.guildporter-checkpoint.json`). Re-running the same command picks up new
  messages since the last run.
- `--skip-bot-role`: name of the bot's own auto-created role, excluded from
  role mirroring.
- A role mapping is saved to `.guildporter-role-mapping.json` for reference.

## FAQ

**Will Discord ban my bot for this?**
Webhook-based migration is a well-established technique and widely used by
server admins. Only migrate servers you administer, and don't spam: the
built-in pacing keeps you inside the documented limits.

**Can I migrate DMs?**
No. Bots can't read DMs they aren't part of, and webhooks don't exist in
DMs.

**What about threads and forum posts?**
Not supported in v1. Thread and forum channels are skipped with a warning.

**How long does it take?**
Roughly 2 seconds per message with attachments taking longer. A few hundred
messages is minutes; tens of thousands is an overnight run, which is what
the checkpoint file is for.

## License

MIT. See [LICENSE](LICENSE).

---
<sub>v1.0.1</sub>
