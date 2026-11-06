# LinkedIn reply bot

Runs on your own computer and your own LinkedIn account. It does two things by itself:

1. Accepts incoming connection requests.
2. Answers direct messages with a short, natural reply.

It is reply-only. It never writes first, and the code has no way to start a conversation or send a connection request. `tests/test_rules.py` checks this.

## How it works

- **Browser:** Playwright drives real Chrome with a dedicated profile. You sign in once by hand, so the bot never stores a password.
- **Requests:** LinkedIn has no official API for this, so every call is a `fetch` made inside the logged-in page. The CSRF token stays in the page.
- **Detection:** events from LinkedIn's realtime stream wake the bot, and a backstop poll is the source of truth. Polling speeds up while a conversation is active.
- **Accepting:** the REST action first, then the newer Dash action, then the Accept button on the page. Only person-to-person invites that arrive after the bot first started are accepted.
- **Replying:** the bot waits for 3 seconds of quiet (9 at most) so a burst gets one reply, shows typing, and checks for newer messages before sending.
- **Model:** any OpenAI-style chat API, by default Groq's free tier. If a model is out of quota the next one in the list answers. The model has no tools, and incoming text is treated as content, never as instructions.

## Guard rails

Replies go out under a real person's name, so nothing is sent unchecked:

- A classifier model checks the incoming message and the drafted reply. The first check runs alongside the draft, so it adds no wait.
- If the check on the draft gives no verdict, or every model is down, a neutral holding line goes out instead. The bot never goes silent, and never sends an unchecked draft.
- Blocked: crime, violence, sexual content, hate, self harm (which gets a caring reply), attack code, questions about how the bot is built, and drafts that commit you to a meeting, money, a job or a deadline. Rudeness is not blocked; it gets a calm reply.
- Rules in `bot/safety.py` need no model, so they hold if the classifier is rate limited. A draft is replaced if it leaks its setup, claims to be human, or contains a link, email or phone number that wasn't already in the chat, which stops a planted phishing link.

`tests/test_guard_flow.py` runs all of this against a fake model.

## Unusual messages

| Message | Behaviour |
|---|---|
| Nearly empty, gibberish | Asks what they meant |
| Emoji only | Short reply in kind |
| Very long | Answers the main point |
| Several in a row | One reply covering all of them |
| Another language | Replies in that language |
| Link | Not opened. Says so and asks what to look at |
| Image, file, voice note, video | Acknowledged by type, asks for the gist |
| Rude | Calm and brief |
| Something it can't know | Says it isn't sure. Never invents facts |
| Meetings, money, personal data | Commits to nothing and shares nothing |
| Prompt injection, odd tasks | Declines lightly |
| "Are you a bot?" | Says yes: an automated assistant is handling messages |
| Recalled or system messages | Skipped |

## Run it

Needs Linux, Google Chrome, [uv](https://docs.astral.sh/uv/) and a free [Groq key](https://console.groq.com/keys).

```bash
uv sync
echo "AI_API_KEY=your-key" > .env
uv run python -m bot login    # sign in by hand in the window that opens
uv run python -m bot doctor   # read-only check
./run.sh                      # blocks sleep and restarts the bot if it exits
```

Optional settings in `.env`: `DRY_RUN=1` logs what it would do without sending or accepting, `REPLY_SCOPE` is covered below, and `AI_BASE_URL`, `AI_MODELS` and `AI_GUARD_MODELS` change the provider. Tests: `uv run pytest`. Reply samples: `uv run python -m tests.eval_replies`.

## Privacy

State and logs live in `~/.local/share/linkedin-bot`, outside this repository. The database stores ids and timestamps, never message text, and logs record counts and timings only. Incoming messages are sent to the AI provider to write the reply. Test data is synthetic.

By default (`REPLY_SCOPE=new_threads`) the bot only speaks in threads that started after it first ran, or to people it accepted, so existing contacts don't get automated replies. `REPLY_SCOPE=all` answers any new one-to-one message.

## What it cannot do yet

- It doesn't open links, view images, read files, or listen to voice notes.
- It knows only your name and headline, so it can't answer questions about your life or work.
- It can't solve a LinkedIn checkpoint or a logout. It alerts on the desktop and waits for you.
- It depends on private LinkedIn endpoints that can change without notice.
- It only runs while the computer is on and online.
- Invitations that arrived before the first start are left for you to handle.
- Group chats and company pages are ignored.

LinkedIn's User Agreement restricts automation, so running this can put the account at risk.

## Credits

Endpoint behaviour was worked out from the live site and from public research, including [mautrix/linkedin](https://github.com/mautrix/linkedin). No code was copied.
