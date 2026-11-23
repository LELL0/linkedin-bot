"""Talk to an Enc Chat bot through the local bot agent (enc-bot-agent).

One file, Python 3.8+, standard library only, no cryptography: the agent
holds the bot's keys and does the end to end encryption; this client speaks
its small HTTP API on 127.0.0.1 or a docker network. Vendor this file into a
project (copy it; keep the version line) rather than installing it.

    from enc_bot_client import Bot, EncBotError

    bot = Bot("http://127.0.0.1:8787", secret=open("/run/secrets/bot").read().strip())
    bot.set_commands([{"command": "report", "description": "The last run"}])
    sent = bot.send("Found 3 jobs", buttons=[[{"label": "Show", "data": "show"}]])
    for ev in bot.events():            # or a webhook: see verify_webhook
        if ev["type"] == "command" and ev["command"] == "report":
            bot.send_file("report.pdf", caption="Today's run", to=ev["chat"])
        elif ev["type"] == "button":
            bot.edit(ev["messageId"], text="Showing", buttons=[])

Events are delivered at least once: skip an event whose "id" you have seen.
"""

import hashlib
import hmac
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

__version__ = "0.5.0"
__all__ = ["Bot", "EncBotError", "verify_webhook"]


class EncBotError(Exception):
    """A refusal from the agent: .code (BOT_NOT_STARTED, RATE_LIMITED, NOT_CONNECTED,
    UNAUTHORIZED, BAD_REQUEST, BAD_BUTTONS, NO_SUCH_MESSAGE, FILE_TOO_LARGE, ...) and .status."""

    def __init__(self, code, message="", status=0):
        super().__init__("%s: %s" % (code, message) if message else code)
        self.code = code
        self.status = status


def verify_webhook(body, header, secret):
    """Whether a webhook POST came from the agent: [body] the raw bytes, [header]
    the X-Enc-Signature value, [secret] the bot's local secret."""
    if not header or not header.startswith("sha256="):
        return False
    if isinstance(body, str):
        body = body.encode("utf-8")
    want = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header[len("sha256="):])


class Bot:
    """One bot of the agent, picked by its local [secret]."""

    def __init__(self, url, secret, timeout=30):
        self.url = url.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self.last_event_id = None

    # ── HTTP ────────────────────────────────────────────────────────────

    def _request(self, method, path, body=None, raw=None, content_type=None, auth=True, timeout=None):
        headers = {}
        data = None
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer " + self.secret
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:
                payload = res.read()
                if res.headers.get("Content-Type", "").startswith("application/json"):
                    return json.loads(payload.decode("utf-8")) if payload else None
                return payload if payload else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(text)
            except ValueError:
                err = {}
            raise EncBotError(err.get("code") or "HTTP_%d" % e.code, err.get("error", ""), e.code) from None
        except (urllib.error.URLError, OSError) as e:
            raise EncBotError("AGENT_UNREACHABLE", str(getattr(e, "reason", e))) from None

    # ── Calls ───────────────────────────────────────────────────────────

    def health(self):
        """{ ok, bots: [{ name, connected, outbox, events, lastError }] }. No secret needed."""
        return self._request("GET", "/v1/health", auth=False)

    def me(self):
        return self._request("GET", "/v1/me")

    def send(self, text, to="owner", buttons=None, reply_to=None):
        """Sends [text] to "owner", "@username" or a user id (one of the bot's people
        who wrote to it). [buttons]: rows of {"label", "data"}. Returns the message id,
        or None when Enc Chat is unreachable and the agent queued it (it goes later,
        within 24 hours, in order)."""
        body = {"to": to, "text": text}
        if buttons is not None:
            body["buttons"] = buttons
        if reply_to:
            body["replyTo"] = reply_to
        out = self._request("POST", "/v1/messages", body)
        return out.get("messageId")

    def edit(self, message_id, text=None, buttons=None):
        """New text and/or buttons on a message the bot sent. buttons=[] removes them."""
        body = {}
        if text is not None:
            body["text"] = text
        if buttons is not None:
            body["buttons"] = buttons
        self._request("PATCH", "/v1/messages/" + urllib.parse.quote(message_id, safe=""), body)

    def delete(self, message_id):
        self._request("DELETE", "/v1/messages/" + urllib.parse.quote(message_id, safe=""))

    def typing(self, to="owner"):
        self._request("POST", "/v1/typing", {"to": to})

    def react(self, message_id, emoji, remove=False):
        self._request("POST", "/v1/reactions", {"messageId": message_id, "emoji": emoji, "remove": remove})

    def set_commands(self, commands):
        """[{"command": "report", "description": "The last run"}] (the "/" menu)."""
        return self._request("PUT", "/v1/commands", commands).get("commands")

    def send_file(self, path, caption="", to="owner", name=None, kind=None, data=None):
        """Sends a file (a path, or [data] bytes with a [name]). [kind] photo, video,
        voice or file; guessed from the name otherwise. Not queued: fails with
        NOT_CONNECTED while Enc Chat is unreachable. Returns the message id."""
        if data is None:
            with open(path, "rb") as f:
                data = f.read()
        name = name or os.path.basename(path or "file")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        fields = [("to", to)]
        if caption:
            fields.append(("caption", caption))
        if kind:
            fields.append(("kind", kind))
        fields.append(("name", name))
        boundary = "encbot" + uuid.uuid4().hex
        parts = []
        for k, v in fields:
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8"))
        safe = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                      % (boundary, safe, mime)).encode("utf-8"))
        parts.append(data)
        parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
        out = self._request("POST", "/v1/files", raw=b"".join(parts),
                            content_type="multipart/form-data; boundary=" + boundary,
                            timeout=max(self.timeout, 60 + len(data) * 8 // 256000))
        return out.get("messageId")

    def download(self, url):
        """The bytes of a file a person sent: the event's file["url"]."""
        return self._request("GET", url, timeout=max(self.timeout, 120))

    # ── Events ──────────────────────────────────────────────────────────

    def events(self, reconnect_delay=2.0, stop=None):
        """Yields events from the agent's stream for ever (a bot set to
        deliver: { stream: true }), reconnecting after a drop and resuming
        after the last event seen. [stop]: a callable; the loop ends when it
        returns True (checked between events and reconnects)."""
        while not (stop and stop()):
            headers = {"Authorization": "Bearer " + self.secret, "Accept": "text/event-stream"}
            if self.last_event_id is not None:
                headers["Last-Event-ID"] = str(self.last_event_id)
            req = urllib.request.Request(self.url + "/v1/events", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    seq, data = None, []
                    for raw in res:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line == "":
                            if data:
                                ev = json.loads("\n".join(data))
                                if seq is not None:
                                    self.last_event_id = seq
                                yield ev
                                if stop and stop():
                                    return
                            seq, data = None, []
                        elif line.startswith("id: "):
                            seq = line[4:]
                        elif line.startswith("data: "):
                            data.append(line[6:])
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise EncBotError("UNAUTHORIZED", "wrong secret", 401) from None
                if e.code == 404:
                    raise EncBotError("NOT_FOUND", "this bot delivers to a webhook", 404) from None
            except (urllib.error.URLError, OSError, ValueError):
                pass
            if stop and stop():
                return
            time.sleep(reconnect_delay)
