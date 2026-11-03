"""Command-line interface for guildporter.

Usage:
    guild-migrate --config mapping.json [--token TOKEN] [--dry-run]
                  [--resume] [--cleanup-webhooks] [--skip-bot-role NAME]
                  [--timestamp-header] [--timestamp-tz America/Chicago]

The bot token can also come from the DISCORD_BOT_TOKEN environment variable.
"""

import argparse
import json
import os
import sys
import time

from . import __version__, api, channels, messages, roles, webhooks


def load_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    for key in ("source_guild", "target_guild", "channels"):
        if key not in config:
            sys.exit("Config error: missing required key '%s'" % key)
    return config


def load_checkpoint(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_checkpoint(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="guild-migrate",
        description="Migrate Discord guild message history via webhook masquerading.",
    )
    parser.add_argument("--config", required=True, help="Path to mapping.json")
    parser.add_argument(
        "--token",
        default=os.environ.get("DISCORD_BOT_TOKEN"),
        help="Bot token (or set DISCORD_BOT_TOKEN)",
    )
    parser.add_argument(
        "--vault",
        action="store_true",
        help="Read the bot token from the Secure Vault (this VM only, "
        "uses the custom.discord-bot credential)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration plan without creating anything",
    )
    parser.add_argument(
        "--checkpoint",
        default=".guildporter-checkpoint.json",
        help="Checkpoint file for resume support",
    )
    parser.add_argument(
        "--cleanup-webhooks",
        action="store_true",
        help="Delete GuildPorter webhooks from target channels after migrating",
    )
    parser.add_argument(
        "--skip-bot-role",
        default=None,
        help="Name of the bot's own role to skip when mirroring roles",
    )
    parser.add_argument(
        "--post-delay",
        type=float,
        default=api.WEBHOOK_POST_FLOOR_SECONDS,
        help="Seconds between webhook posts (rate-limit safety, default %(default)s)",
    )
    parser.add_argument(
        "--timestamp-header",
        action="store_true",
        help="Prepend a header line with each message's original "
        "timestamp ([DD-MM-YY HH:MM:SS]) to the copied message",
    )
    parser.add_argument(
        "--timestamp-tz",
        default="UTC",
        help="IANA timezone for --timestamp-header (default %(default)s)",
    )
    parser.add_argument("--version", action="version", version="guildporter " + __version__)
    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.vault:
        client = api.DiscordAPI.from_vault()
    else:
        if not args.token:
            sys.exit("No bot token: pass --token, set DISCORD_BOT_TOKEN, or use --vault")
        client = api.DiscordAPI(args.token)

    source_guild = config["source_guild"]
    target_guild = config["target_guild"]

    print("guildporter %s" % __version__)
    if args.dry_run:
        print("DRY RUN - nothing will be created\n")

    # 1. Roles -----------------------------------------------------------
    role_mapping = []
    if config.get("migrate_roles", True):
        print("Step 1/4: mirroring roles")
        if args.dry_run:
            print("  [dry-run] would mirror roles (skipping @everyone and managed roles)")
        else:
            skip = [args.skip_bot_role] if args.skip_bot_role else []
            role_mapping = roles.mirror_roles(client, source_guild, target_guild, skip)
            with open(".guildporter-role-mapping.json", "w", encoding="utf-8") as handle:
                json.dump(role_mapping, handle, indent=2)
            print("  Saved role mapping to .guildporter-role-mapping.json")
    else:
        print("Step 1/4: skipping role migration (migrate_roles=false)")

    # 2. Channels --------------------------------------------------------
    print("\nStep 2/4: resolving source channels")
    entries = []
    for item in config["channels"]:
        ref = item.get("source")
        source = channels.find_channel(client, source_guild, str(ref))
        if not source:
            print('  WARNING: source channel "%s" not found, skipping' % ref)
            continue
        entries.append(
            {
                "source_channel": source,
                "target": item.get("target") or source["name"],
                "category": item.get("category"),
            }
        )
    print("  Resolved %d of %d channels" % (len(entries), len(config["channels"])))
    if args.dry_run:
        for entry in entries:
            print(
                '  [dry-run] "%s" -> "%s"%s'
                % (
                    entry["source_channel"]["name"],
                    entry["target"],
                    ' (category "%s")' % entry["category"] if entry["category"] else "",
                )
            )

    channel_map = {}
    if config.get("migrate_channels", True):
        print("\nStep 3/4: creating target channels")
        channel_map = channels.build_channels(
            client, target_guild, entries, role_mapping, dry_run=args.dry_run
        )
    else:
        print("\nStep 3/4: skipping channel creation (migrate_channels=false);")
        print("  using explicit target channel IDs from config")
        for entry, item in zip(entries, config["channels"]):
            if str(item.get("target", "")).isdigit():
                channel_map[entry["source_channel"]["id"]] = item["target"]

    if args.dry_run:
        print("\nDry run complete. Re-run without --dry-run to migrate.")
        return 0

    # 3. Messages --------------------------------------------------------
    print("\nStep 4/4: migrating message history")
    checkpoint = load_checkpoint(args.checkpoint)
    total_migrated = 0
    total_skipped = 0
    migrated_channels = []

    for entry in entries:
        source_id = entry["source_channel"]["id"]
        target_id = channel_map.get(source_id)
        if not target_id:
            print('  Skipping "%s": no target channel' % entry["source_channel"]["name"])
            continue

        hook = webhooks.get_or_create(client, target_id)
        if not hook:
            print('  Skipping "%s": webhook unavailable' % entry["source_channel"]["name"])
            continue

        name = entry["source_channel"]["name"]
        print('Migrating #%s ...' % name)

        def progress(done, skipped, label=name):
            if (done + skipped) % 25 == 0:
                print("    ... %d migrated, %d skipped" % (done, skipped))

        migrated, skipped = messages.migrate_channel(
            client, source_id, hook, checkpoint, on_progress=progress,
            min_interval=args.post_delay,
            timestamp_tz=args.timestamp_tz if args.timestamp_header else None,
        )
        save_checkpoint(args.checkpoint, checkpoint)
        migrated_channels.append(target_id)
        total_migrated += migrated
        total_skipped += skipped
        print("  #%s: %d migrated, %d skipped" % (name, migrated, skipped))
        time.sleep(1)

    print(
        "\nDone: %d messages migrated, %d skipped across %d channels."
        % (total_migrated, total_skipped, len(migrated_channels))
    )
    print("Checkpoint saved to %s (re-run to resume or pick up new messages)." % args.checkpoint)

    # 4. Optional webhook cleanup ----------------------------------------
    if args.cleanup_webhooks:
        print("\nCleaning up GuildPorter webhooks...")
        for target_id in migrated_channels:
            removed = webhooks.cleanup(client, target_id)
            print("  Removed %d webhook(s) from %s" % (removed, target_id))

    return 0


if __name__ == "__main__":
    sys.exit(main())
