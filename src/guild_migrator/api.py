"""Thin Discord REST API v10 client.

Only the standard library is used (urllib). All rate-limit handling lives
here: 429 responses are honored via ``retry_after`` (plus a small safety
margin), and transient 5xx responses are retried with exponential backoff.
"""

import json
import time
import urllib.error
import urllib.request

BASE_URL = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/mortis2600/guildporter, 1.0.1)"

# Webhook executions are rate-limited harder than normal calls (~30/min per
# webhook). Keep a conservative floor between posts to the same webhook.
# 2.5s spacing = 24 posts/min, comfortably under the documented limit.
WEBHOOK_POST_FLOOR_SECONDS = 2.5

# Safety margin added whenever we proactively sleep on rate-limit headers.
RATE_LIMIT_SAFETY_MARGIN = 0.5


def rate_limit_delay(headers):
    """Proactive delay from Discord's X-RateLimit response headers.

    Returns seconds to wait (0 when no wait is needed). When the bucket is
    exhausted (remaining == 0), we sleep until Discord says it resets, plus
    a small safety margin, instead of firing into a guaranteed 429.
    ``headers`` is a case-insensitive mapping (e.g. response.headers).
    """
    remaining = headers.get("X-RateLimit-Remaining")
    reset_after = headers.get("X-RateLimit-Reset-After")
    if remaining is None or reset_after is None:
        return 0.0
    try:
        if int(remaining) > 0:
            return 0.0
        return float(reset_after) + RATE_LIMIT_SAFETY_MARGIN
    except (ValueError, TypeError):
        return 0.0


def compute_delay(attempt, retry_after):
    """Delay in seconds before retrying a failed request.

    ``retry_after`` comes from Discord's 429 body (seconds). When absent we
    fall back to exponential backoff capped at 60 seconds. A 0.5s safety
    margin is added so we never land exactly on the bucket edge.
    """
    if retry_after is not None:
        return float(retry_after) + 0.5
    return min(2.0 ** attempt, 60.0)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: API calls should never redirect; if one does,
    the endpoint is wrong and silently following it could leak the
    Authorization header to another host."""

    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


class DiscordAPI:
    """Authenticated client for Discord's REST API."""

    def __init__(self, token, user_agent=USER_AGENT, timeout=30):
        # token may be a raw bot token ("abc..."), an already-prefixed one
        # ("Bot abc..."), or an authd surrogate ("hsurr:..."). Surrogates
        # must go out VERBATIM as the whole header value: authd swaps the
        # exact string for the vault secret (which stores "Bot <token>").
        # Adding any prefix around a surrogate breaks the swap.
        token = token.strip()
        if token.startswith("hsurr:") or token.startswith("Bot "):
            self.auth_header = token
        else:
            self.auth_header = "Bot " + token
        self.user_agent = user_agent
        self.timeout = timeout

    @classmethod
    def from_vault(cls, user_agent=USER_AGENT, timeout=30):
        """Create an API client using the Secure Vault credential.

        VM-specific: reads the ``custom.discord-bot`` surrogate via
        dynamic_credentials; authd swaps it for the real secret
        ("Bot <token>") on the outbound request. The token never touches
        disk, argv, or the environment.
        """
        import sys

        sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
        from dynamic_credentials import dynamic_credential_entry, ensure_allowed_url

        ensure_allowed_url(BASE_URL, ["discord.com"])
        entry = dynamic_credential_entry("custom.discord-bot")
        return cls(str(entry["surrogate"]).strip(),
                   user_agent=user_agent, timeout=timeout)

    def request(self, method, path, data=None, max_attempts=6):
        """Perform an API request, returning the decoded JSON body.

        Returns ``None`` on unrecoverable client errors (4xx other than 429)
        after logging the failure to stdout.
        """
        body = json.dumps(data).encode("utf-8") if data is not None else None
        url = BASE_URL + path

        for attempt in range(max_attempts):
            req = urllib.request.Request(
                url,
                data=body,
                method=method,
                headers={
                    "Authorization": self.auth_header,
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                },
            )
            try:
                opener = urllib.request.build_opener(NoRedirect())
                with opener.open(req, timeout=self.timeout) as resp:
                    delay = rate_limit_delay(resp.headers)
                    if resp.status == 204:
                        if delay:
                            print("  Bucket exhausted, proactively waiting %.1fs..." % delay)
                            time.sleep(delay)
                        return {"success": True}
                    raw = resp.read().decode("utf-8")
                    if delay:
                        print("  Bucket exhausted, proactively waiting %.1fs..." % delay)
                        time.sleep(delay)
                    return json.loads(raw) if raw else {"success": True}
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    retry_after = None
                    try:
                        payload = json.loads(exc.read().decode("utf-8"))
                        retry_after = payload.get("retry_after")
                    except Exception:
                        pass
                    delay = compute_delay(attempt, retry_after)
                    print("  Rate limited (429), waiting %.1fs..." % delay)
                    time.sleep(delay)
                    continue
                if 500 <= exc.code < 600:
                    delay = compute_delay(attempt, None)
                    print("  Server error %d, retrying in %.1fs..." % (exc.code, delay))
                    time.sleep(delay)
                    continue
                print("  API error %d on %s %s" % (exc.code, method, path))
                return None
            except Exception as exc:  # network hiccup, DNS, timeout
                delay = compute_delay(attempt, None)
                print("  Network error (%s), retrying in %.1fs..." % (exc, delay))
                time.sleep(delay)
        print("  Gave up on %s %s after %d attempts" % (method, path, max_attempts))
        return None

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, data):
        return self.request("POST", path, data)

    def patch(self, path, data):
        return self.request("PATCH", path, data)

    def delete(self, path):
        return self.request("DELETE", path)

    def paginate_messages(self, channel_id, limit=100):
        """Yield every message in a channel, oldest first.

        Discord returns messages newest-first in pages of up to 100; we walk
        backwards with ``before`` and reverse each page.
        """
        before = None
        while True:
            path = "/channels/%s/messages?limit=%d" % (channel_id, limit)
            if before:
                path += "&before=" + before
            page = self.get(path)
            if not page:
                return
            if not isinstance(page, list) or len(page) == 0:
                return
            for message in reversed(page):
                yield message
            before = page[-1]["id"]
            if len(page) < limit:
                return
            time.sleep(0.5)  # be gentle on the history endpoint
