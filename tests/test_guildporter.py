"""Unit tests for guildporter. No network access; pure logic only."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from guild_migrator.api import DiscordAPI, compute_delay, rate_limit_delay
from guild_migrator.messages import build_payload, split_content
from guild_migrator.roles import remap_overwrites


def test_compute_delay_honors_retry_after():
    assert compute_delay(0, 5.0) == 5.5
    assert compute_delay(3, 0.2) == 0.7


def test_compute_delay_backoff_without_retry_after():
    assert compute_delay(0, None) == 1.0
    assert compute_delay(1, None) == 2.0
    assert compute_delay(2, None) == 4.0
    assert compute_delay(10, None) == 60.0  # capped


def test_split_content_short():
    assert split_content("hello") == ["hello"]
    assert split_content("") == []
    assert split_content(None) == []


def test_split_content_long_line_gets_hard_cut():
    chunks = split_content("x" * 4500)
    assert len(chunks) == 3
    assert all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == "x" * 4500


def test_split_content_multiline():
    lines = ["line %d" % i for i in range(500)]
    chunks = split_content("\n".join(lines))
    assert all(len(c) <= 2000 for c in chunks)
    assert "\n".join(chunks) == "\n".join(lines)


def test_build_payload_prefers_nick():
    message = {
        "author": {
            "id": "1",
            "username": "dan",
            "global_name": "Dan Global",
            "avatar": "abc123",
        },
        "member": {"nick": "Dan the Deli Guy"},
        "content": "hello",
    }
    payload, content = build_payload(message)
    assert payload["username"] == "Dan the Deli Guy"
    assert payload["avatar_url"] == "https://cdn.discordapp.com/avatars/1/abc123.png"
    assert payload["allowed_mentions"] == {"parse": []}
    assert content == "hello"


def test_build_payload_falls_back_to_username():
    message = {"author": {"id": "2", "username": "wes"}, "content": "hi"}
    payload, _ = build_payload(message)
    assert payload["username"] == "wes"
    assert "avatar_url" not in payload


def test_build_payload_truncates_long_usernames():
    message = {"author": {"username": "n" * 200}, "content": "hi"}
    payload, _ = build_payload(message)
    assert len(payload["username"]) == 80


def test_remap_overwrites():
    mapping = [{"old_id": "10", "new_id": "20", "name": "Staff"}]
    overwrites = [
        {"id": "10", "type": 0, "allow": "0", "deny": "0"},
        {"id": "99", "type": 0, "allow": "0", "deny": "0"},  # not mirrored -> dropped
        {"id": "555", "type": 1, "allow": "0", "deny": "0"},  # member overwrite kept
    ]
    result = remap_overwrites(overwrites, mapping)
    assert len(result) == 2
    assert result[0]["id"] == "20"
    assert result[1] == {"id": "555", "type": 1, "allow": "0", "deny": "0"}


def test_remap_overwrites_empty():
    assert remap_overwrites(None, []) == []
    assert remap_overwrites([], []) == []


def test_rate_limit_delay_waits_when_bucket_exhausted():
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "12.3"}
    assert rate_limit_delay(headers) == 12.8  # reset-after + 0.5s margin


def test_rate_limit_delay_no_wait_when_remaining():
    headers = {"X-RateLimit-Remaining": "5", "X-RateLimit-Reset-After": "12.3"}
    assert rate_limit_delay(headers) == 0.0


def test_rate_limit_delay_no_wait_without_headers():
    assert rate_limit_delay({}) == 0.0
    assert rate_limit_delay({"X-RateLimit-Remaining": "0"}) == 0.0


def test_rate_limit_delay_tolerates_garbage():
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "soon"}
    assert rate_limit_delay(headers) == 0.0


def test_auth_header_raw_token_gets_prefix():
    assert DiscordAPI("abc123").auth_header == "Bot abc123"


def test_auth_header_prefixed_token_untouched():
    assert DiscordAPI("Bot abc123").auth_header == "Bot abc123"


def test_auth_header_surrogate_goes_out_verbatim():
    # authd swaps the exact surrogate string for the vault secret
    # (which stores "Bot <token>"); any added prefix breaks the swap.
    assert DiscordAPI("hsurr:deadbeef").auth_header == "hsurr:deadbeef"


def test_auth_header_strips_whitespace():
    assert DiscordAPI("  abc123\n").auth_header == "Bot abc123"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print("PASS %s" % test.__name__)
        except AssertionError as exc:
            failed += 1
            print("FAIL %s: %s" % (test.__name__, exc))
    print("\n%d passed, %d failed" % (len(tests) - failed, failed))
    sys.exit(1 if failed else 0)


def test_timestamp_header_utc():
    from guild_migrator.messages import timestamp_header

    message = {
        "author": {"id": "3", "username": "khrysteenah"},
        "timestamp": "2026-11-03T02:22:41.123456+00:00",
        "content": "availability?",
    }
    assert timestamp_header(message, "UTC") == "[03-11-26 02:22:41]"


def test_timestamp_header_converts_timezone():
    from guild_migrator.messages import timestamp_header

    message = {
        "author": {"id": "3", "username": "khrysteenah"},
        "member": {"nick": "Christina"},
        "timestamp": "2026-11-03T02:22:41+00:00",
        "content": "availability?",
    }
    # November in Chicago is CST (UTC-6). No author name in the header:
    # the webhook already posts under the author's name.
    assert timestamp_header(message, "America/Chicago") == "[02-11-26 20:22:41]"


def test_timestamp_header_missing_timestamp():
    from guild_migrator.messages import timestamp_header

    assert timestamp_header({"author": {"username": "x"}}, "UTC") is None


def test_build_payload_with_timestamp_header():
    message = {
        "author": {"id": "3", "username": "khrysteenah"},
        "timestamp": "2026-11-03T02:22:41+00:00",
        "content": "hello",
    }
    _, content = build_payload(message, timestamp_tz="UTC")
    assert content == "[03-11-26 02:22:41]\nhello"


def test_build_payload_timestamp_header_on_empty_content():
    message = {
        "author": {"id": "3", "username": "khrysteenah"},
        "timestamp": "2026-11-03T02:22:41+00:00",
        "content": "",
        "attachments": [{"filename": "weekly_schedule.png"}],
    }
    _, content = build_payload(message, timestamp_tz="UTC")
    assert content == "[03-11-26 02:22:41]"


def test_build_payload_default_has_no_header():
    message = {
        "author": {"id": "3", "username": "khrysteenah"},
        "timestamp": "2026-11-03T02:22:41+00:00",
        "content": "hello",
    }
    _, content = build_payload(message)
    assert content == "hello"
