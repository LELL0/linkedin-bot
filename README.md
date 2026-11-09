# LinkedIn reply bot

Runs on your own computer and your own LinkedIn account. It does two things by itself:

1. Accepts incoming connection requests.
2. Answers direct messages with a short, natural reply.

It is reply-only. It never writes first, and the code has no way to start a conversation or send a connection request. `tests/test_rules.py` checks this.

## How it works

- **Browser:** Playwright drives real Chrome with a dedicated profile. You sign in once by hand, so the bot never stores a password.
- **Requests:** LinkedIn has no official API for this, so every call is a `fetch` made inside the logged-in page. The CSRF token stays in the page.
- **Detection:** the inbox is checked every 5-15 minutes; invitations once a day in the morning.
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

## Staying under LinkedIn's radar

LinkedIn signs out and restricts accounts that make requests at a machine's pace. The bot
is slow on purpose, and each limit can be changed in `.env`:

| Setting | Default | What it does |
|---|---|---|
| `ACTIVE_HOURS` | `8-24` | Local hours when the bot works. Outside them it makes no requests; what arrived is answered in the morning. `8-2` runs past midnight. |
| `POLL_S` | `300-900` | Inbox check every 5-15 minutes, random. Realtime events do not make it sooner. |
| `MIN_CHECK_GAP_S` | `60` | Never two inbox checks closer than this. |
| `REPLY_DELAY_S` | `25-150` | Wait before reading and answering, plus typing time for the reply's length. |
| `MAX_REPLIES_PER_DAY` | `40` | Then the rest wait until tomorrow, with a Telegram alert. |
| `INVITE_WINDOW` | `9-11` | Invitations are handled once a day, at a random time in this window: every pending one is accepted. |
| `ACCEPT_GAP_S` | `20-90` | Pause between two accepts in that daily round. |
| `MAX_ACCEPTS_PER_DAY` | `0` | 0 accepts them all; a number caps the daily round. |
| `CATCHUP_GAP_S` | `90-300` | Pause between two conversations answered by `/catchup`. |

Older conversations (started before the bot) are never answered on their own. Send
`/catchup 3` on Telegram to answer every unanswered one-to-one message from the last 3 days
(1-30), one conversation at a time, within the active hours and the daily reply limit. The bot
first sends the list of names. A reply to a message older than 3 hours may say sorry for the
late answer. `/catchup stop` cancels.

When LinkedIn rejects the session (401, 403, a redirect to the login page), every loop stops
until someone signs in again, and then waits 5 minutes before the first request. HTTP 429,
999 and 5xx answers stop all requests for 5 minutes, doubling each time in a row up to 4 hours.
Chrome starts without Playwright's `--enable-automation` flag.

Run the bot from the network the account normally uses. A session copied to a cloud
server shows up from a datacenter IP in another country, which LinkedIn treats as suspicious.

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
| "Are you a bot?" | Neither confirms nor denies, and moves the chat on. It never claims to be human |
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

## Run it on a server

`deploy/` runs the bot in Docker with Chromium in a virtual screen, so it works on amd64 and arm64 servers with no desktop. Settings go in `deploy/.env` (`AI_API_KEY`, and `TG_BOT_TOKEN` plus `TG_CHAT_ID` for Telegram alerts).

```bash
cd deploy
APP_UID=$(id -u) docker compose up -d --build
ssh -N -L 5901:localhost:5901 your-server       # from your computer, then VNC to localhost:5901
cat data/vnc-password                           # VNC password, created on first start
```

Sign in to LinkedIn once in the window you see over VNC; the bot carries on by itself. Data (Chrome profile, state, logs, timings) stays in `deploy/data`.

Health:
- `docker compose ps` shows `healthy` or `unhealthy`. The check (`python -m bot health`) fails when the bot is signed out, its loop stops, or no inbox check has succeeded for the longest poll wait (`POLL_S` max) plus 6 minutes.
- A watchdog thread exits the process if the loop stalls, and the bot exits after 10 minutes without a successful check, so Docker's restart policy starts it fresh.
- Sign-outs, stalls and failed sends go to Telegram, at most once an hour per problem.
- `/verbose 0-5` sends the bot's log to Telegram while it runs: 0 off (only alerts), 1 warnings and errors (every 5 min), 2 also each accept and reply (2 min), 3 everything but the heartbeat (1 min), 4 also the heartbeat (30 s), 5 also debug lines and other libraries (30 s). Lines from httpx are never sent, because they hold the bot token. `/verbose` alone shows the level; it survives restarts.
- `/settings` lists the settings below that can change while the bot runs, with their values. `/set name value` changes one (`/set poll 5-15`, `/set reply_delay 30-120`, `/set max_replies 30`, `/set dry_run on`, `/set scope all`), `/interval 5-15` is short for `/set poll`, and `/reset name` or `/reset all` goes back to the environment's value. Ranges are random between the two numbers, in the unit `/settings` shows (min, s or h). Changes are kept in the bot's database across restarts; a new poll interval also replaces the wait already going. Names: `poll`, `check_gap`, `reply_delay`, `active_hours`, `invite_window`, `accept_gap`, `catchup_gap`, `max_replies`, `max_accepts`, `backoff`, `max_backoff`, `dry_run`, `scope`.

Run only one copy per LinkedIn account: two would both answer every message.

## Timings

Every accept and reply is written to `~/.local/share/linkedin-bot/metrics.jsonl`, with timings only and no message text.

```bash
uv run python -m bot stats                       # median, p95 and max for each stage
tail -f ~/.local/share/linkedin-bot/bot.log      # live view
```

Each reply is split into noticing it, waiting for quiet, the model draft, the draft check and sending. Each record also shows whether realtime or the poll noticed it, which model answered, and how the reply was decided.

## Privacy

State and logs live in `~/.local/share/linkedin-bot`, outside this repository. The database stores ids and timestamps, never message text, and logs record counts and timings only. Incoming messages are sent to the AI provider to write the reply. Test data is synthetic.

By default (`REPLY_SCOPE=new_threads`) the bot only speaks in threads that started after it first ran, or to people it accepted, from the moment it accepted them. Other existing contacts don't get automated replies. `REPLY_SCOPE=all` answers any new one-to-one message.

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
