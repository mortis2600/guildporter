"""Webhook lifecycle and message execution.

For each target channel we create (or reuse) one webhook named
``GuildPorter``. Messages are executed through the webhook's token URL with
``?wait=true`` so we get the created message back (useful for verification).

Attachments are re-uploaded: the original file is downloaded into memory and
sent as multipart/form-data alongside the JSON payload.
"""

from .api import USER_AGENT

import io
import json
import time
import urllib.request

from .api import WEBHOOK_POST_FLOOR_SECONDS, rate_limit_delay

WEBHOOK_NAME = "GuildPorter"


def get_or_create(api, channel_id):
    """Return an existing GuildPorter webhook for the channel, else create one."""
    existing = api.get("/channels/%s/webhooks" % channel_id) or []
    for hook in existing:
        if hook.get("name") == WEBHOOK_NAME and hook.get("token"):
            return hook
    created = api.post("/channels/%s/webhooks" % channel_id, {"name": WEBHOOK_NAME})
    if created and created.get("token"):
        print("  Created webhook in channel %s" % channel_id)
        return created
    print("  FAILED to create webhook in channel %s" % channel_id)
    return None


def cleanup(api, channel_id):
    """Delete all GuildPorter webhooks in a channel."""
    existing = api.get("/channels/%s/webhooks" % channel_id) or []
    removed = 0
    for hook in existing:
        if hook.get("name") == WEBHOOK_NAME:
            api.delete("/webhooks/%s" % hook["id"])
            removed += 1
            time.sleep(0.3)
    return removed


def _encode_multipart(payload_json, files):
    """Build a multipart/form-data body.

    ``files`` is a list of (filename, bytes, content_type). Returns
    (body_bytes, content_type_header).
    """
    boundary = "GuildPorterBoundary1a2b3c"
    buf = io.BytesIO()

    def write_line(text):
        buf.write(text.encode("utf-8") + b"\r\n")

    write_line("--" + boundary)
    write_line('Content-Disposition: form-data; name="payload_json"')
    write_line("Content-Type: application/json")
    write_line("")
    buf.write(payload_json.encode("utf-8") + b"\r\n")

    for index, (filename, data, content_type) in enumerate(files):
        write_line("--" + boundary)
        write_line(
            'Content-Disposition: form-data; name="files[%d]"; filename="%s"'
            % (index, filename.replace('"', "_"))
        )
        write_line("Content-Type: %s" % content_type)
        write_line("")
        buf.write(data + b"\r\n")

    write_line("--" + boundary + "--")
    return buf.getvalue(), "multipart/form-data; boundary=" + boundary


def execute(webhook, payload, files=(), min_interval=WEBHOOK_POST_FLOOR_SECONDS):
    """Post one message through the webhook. Returns the created message or None.

    ``min_interval`` floors the spacing between posts to the same webhook
    (rate-limit safety). Discord's X-RateLimit headers are also honored
    proactively, and 429s are retried via ``retry_after``.
    """
    url = "https://discord.com/api/v10/webhooks/%s/%s?wait=true" % (
        webhook["id"],
        webhook["token"],
    )
    payload_json = json.dumps(payload)

    for attempt in range(5):
        if files:
            body, content_type = _encode_multipart(payload_json, files)
        else:
            body, content_type = payload_json.encode("utf-8"), "application/json"

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": content_type,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                delay = rate_limit_delay(resp.headers)
                if delay:
                    print("    Webhook bucket exhausted, waiting %.1fs..." % delay)
                    time.sleep(delay)
                time.sleep(min_interval)
                return json.loads(raw) if raw else {"success": True}
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                try:
                    retry_after = json.loads(exc.read().decode("utf-8")).get(
                        "retry_after", 5
                    )
                except Exception:
                    retry_after = 5
                print("    Webhook rate limited, waiting %.1fs..." % (retry_after + 0.5))
                time.sleep(retry_after + 0.5)
                continue
            print("    Webhook execute failed: HTTP %d" % exc.code)
            return None
        except Exception as exc:
            print("    Webhook execute network error: %s" % exc)
            time.sleep(2.0)
    print("    Webhook execute gave up after 5 attempts")
    return None


def download_attachment(url, max_bytes=25 * 1024 * 1024):
    """Download an attachment into memory. Returns (filename, bytes, content_type) or None."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read(max_bytes + 1)
            if len(data) > max_bytes:
                return None  # too large to re-upload; caller skips with a warning
            filename = url.rsplit("/", 1)[-1].split("?")[0] or "attachment"
            content_type = resp.headers.get_content_type() or "application/octet-stream"
            return filename, data, content_type
    except Exception as exc:
        print("    Attachment download failed (%s): %s" % (url, exc))
        return None
