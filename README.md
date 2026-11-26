# LinkedIn reply bot

Runs on your own computer and your own LinkedIn account. It does two things by itself:

1. Accepts incoming connection requests.
2. Answers direct messages with a short, natural reply.

It is reply-only. It never writes first, and the code has no way to start a conversation or send a connection request. `tests/test_rules.py` checks this.

## How it works

- **Browser:** Playwright drives real Chrome with a dedicated profile. You sign in once by hand, so the bot never stores a password.
- **Requests:** LinkedIn has no official API for this, so every call is a `fetch` made inside the logged-in page. The CSRF token stays in the page.
- **Detection:** like a person who opens LinkedIn a few times a day. The inbox is checked 3 times a day and invitations twice, at random moments that change every day, with Chrome closed in between. Once a message is seen and answered, the inbox is checked every 2-10 minutes until 2 hours pass with no new message, then back to 3 a day.
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
| `INBOX_CHECKS` | `3` | Inbox checks a day when no chat goes on. The active hours are cut in equal parts and each check falls at a random point of its part, away from the edges (with `8-24`: 8:48-12:32, 14:08-17:52, 19:28-23:12, at least 1.6 hours apart). The moments change every day and a restart keeps them. Realtime events do not make a check sooner. |
| `POLL_S` | `120-600` | While a chat goes on: a random 2-10 minutes between inbox checks. |
| `WATCH_S` | `7200` | A chat goes on until this long after its latest message or reply; then the next check is a planned one again. |
| `MIN_CHECK_GAP_S` | `60` | Never two inbox checks closer than this. |
| `REPLY_DELAY_S` | `25-150` | Wait before reading and answering, plus typing time for the reply's length. |
| `MAX_REPLIES_PER_DAY` | `40` | Then the rest wait until tomorrow, with an Enc Chat alert. |
| `INVITE_ROUNDS` | `2` | Invitation rounds a day, planned like the inbox checks (with `8-24`: 9:12-14:48 and 17:12-22:48): every pending one is accepted. |
| `ACCEPT_GAP_S` | `20-90` | Pause between two accepts in a round. |
| `MAX_ACCEPTS_PER_DAY` | `0` | 0 accepts them all; a number caps the daily round. |
| `CATCHUP_GAP_S` | `90-300` | Pause between two conversations answered by `/catchup`. |
| `BOT_PROXY` | empty | e.g. `socks5://172.17.0.1:1080`: every LinkedIn request goes through this proxy (a tunnel to your home connection, see below). When set, Chrome opens only while the proxy answers and never falls back to the server's own address; WebRTC is kept inside the proxy too. |
| | | Chrome opens only for a check, an invitation round, a reply or a catch-up, and closes about a minute after (it stays open while a chat goes on, or when the next check is under 10 minutes away). No LinkedIn tab is connected at night or between checks, and the page is never reloaded on a timer. |

Older conversations (started before the bot) are never answered on their own. Send
`/catchup 3` in Enc Chat to answer every unanswered one-to-one message from the last 3 days
(1-30), one conversation at a time, within the active hours and the daily reply limit. The bot
first sends the list of names. A reply to a message older than 3 hours may say sorry for the
late answer. `/catchup stop` cancels and says how many were answered.

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

`deploy/` runs the bot in Docker with Chromium in a virtual screen, so it works on amd64 and arm64 servers with no desktop. Settings go in `deploy/.env` (`AI_API_KEY`, and `ENC_BOT_SECRET` for the Enc Chat bot `bot_linkedin`; `ENC_BOT_URL` defaults to `http://enc-bot-agent:8787`). The container joins the external docker network `enc-bots`, where the bot agent runs (stream mode: no port to open). The bot takes commands and sends alerts only through Enc Chat; it sends nothing to Telegram (the old `TG_BOT_TOKEN` and `TG_CHAT_ID` are not read). `ENC_OWNER` is required: your Enc user id (26 characters) or `@username`. Without it, or if it is malformed, the bot ignores every command and logs `ENC_OWNER_MISSING` once at start.

```bash
cd deploy
APP_UID=$(id -u) docker compose up -d --build
ssh -N -L 5901:localhost:5901 your-server       # from your computer, then VNC to localhost:5901
cat data/vnc-password                           # VNC password, created on first start
```

Before a deploy, or a pull request to `main`, run `scripts/check.sh --docker` on your computer. It checks the last commit in a clean clone, the way CI does (no `.env`, no uncommitted edits): `uv sync --locked`, the tests, the pre-commit hooks and gitleaks over the whole history, which are the `test` and `secrets` checks `main` requires. `--docker` also builds the server image, imports every module in it and starts its Chromium; add `--platform linux/arm64` to build for an arm64 server. `tests/test_deploy.py` keeps `deploy/` in step with the code, since CI never builds the image: the Playwright version in the `FROM` tag, the pip pin and `uv.lock` must match, every imported package must be installed in the image, and VNC stays on the server's loopback.

### LinkedIn through your home connection

A cloud server's address is the strongest bot signal LinkedIn sees: the account was signed out twice in two days (2026-11-08, 11-09) while the bot ran from an Oracle datacenter in Frankfurt and the owner used LinkedIn from home. Send the bot's LinkedIn traffic through your home connection instead, with a reverse SOCKS tunnel from a computer at home that stays on:

1. On the server, once: let the tunnel listen on Docker's bridge address and let containers reach it.
   - `/etc/ssh/sshd_config`: `GatewayPorts clientspecified`, then `sudo systemctl reload ssh`.
   - `sudo iptables -I INPUT -s 172.16.0.0/12 -d 172.17.0.1 -p tcp --dport 1080 -j ACCEPT` and `sudo netfilter-persistent save` (Oracle images reject other input). 172.17.0.1 is not reachable from the internet.
2. At home: `ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 172.17.0.1:1080 your-server` (OpenSSH 7.6+ acts as the SOCKS proxy itself). Keep it running, e.g. as a systemd user service with `Restart=always`.
3. In `deploy/.env`: `BOT_PROXY=socks5://172.17.0.1:1080`, then recreate the container and sign in again over VNC, so the new session starts from your home address.

When the home computer is off the bot waits (state `no_proxy`, one Enc Chat alert) instead of using the server's address.

Sign in to LinkedIn once in the window you see over VNC; the bot carries on by itself. Data (Chrome profile, state, logs, timings) stays in `deploy/data`.

Health:
- `docker compose ps` shows `healthy` or `unhealthy`. The check (`python -m bot health`) fails when the bot is signed out, its loop stops, or a due inbox check has not succeeded for the longest wait between tries (`POLL_S` max or 15 minutes, whichever is longer) plus 6 minutes. The hours between planned checks do not count.
- A watchdog thread exits the process if the loop stalls, and the bot exits after 10 minutes without a successful check, so Docker's restart policy starts it fresh.
- Sign-outs, stalls and failed sends go to Enc Chat, at most once an hour per problem.
- Messages to Enc Chat follow the Enc Chat bot style (v2, built on Enc Chat's "Keep bot messages small"). The first line is a state emoji and `*LinkedIn · state*` in bold, then at most one fact: `🟢 *LinkedIn · running* · checked 5m ago`. The emoji says the state: 🟢 all well, 🟡 needs a look (paused, dry run, a limit, a push-back), 🔴 down until you act (signed out, home tunnel down), ✅ done, ❌ failed, 🆕 new (the `/replies` feed), ℹ️ information, ⏳ in progress. Each part after it is one line that starts with its name in bold, at most three facts joined by ` · ` (`*Since start* 3 accepts · 7 replies`). No line is longer than 70 characters (two lines of a phone's chat bubble), and there are no headings, blank lines or dividers; a `-` list only for log lines, `>` quotes for what someone wrote. Where three or more things share the same facts the message carries a table instead: a code box with a header in capitals, numbers lined up on the right, at most 32 characters a line (a phone's bubble in the app's monospace font), at most 12 rows and two boxes a message (`marks.table()`). Durations read `4.2s`, `42s`, `4m 12s`, `2h 5m`; ages `7h ago`; clock times are on your clock in Beirut (`18:40`), whatever the container's TZ. A wrong argument gets one line that says what to type (`Use /verbose 0-5`), an unknown command `I don't know /foo · /help lists what I can do`. Enc Chat 1.7.14 and later draws the marks (`bot/marks.py`); older apps show them as typed. Names, messages and error strings are made inert and kept on their lines first, so they cannot add a heading, a list, a link or bold of their own. `tests/test_enc.py` checks every answer and alert against these rules.
- `/help` (and `/start`) lists one command a line, most used first, with the same words as the app's "/" menu (both come from `COMMANDS` in `bot/enc.py`): `/status`, `/pause`, `/resume`, `/stats`, `/replies on/off`, `/catchup 1-30`, `/verbose 0-5`, `/settings`, `/set name value`, `/reset name/all`, `/help`.
- `/stats` shows when the latest accept and reply happened and how many replies the AI wrote versus a canned line, then two tables: accepts and replies a day for the last 7 days (today first), and how long accepts and replies take (typical and 95% within) with where a reply's time goes (seen, waited, AI). With fewer than three timings they stay one line each. `python -m bot stats` prints the full detail.
- `/verbose 0-5` sends the bot's log to Enc Chat while it runs: 0 off (only alerts), 1 warnings and errors (every 5 min), 2 also each accept and reply (2 min), 3 everything but the heartbeat (1 min), 4 also the heartbeat (30 s), 5 also debug lines and other libraries (30 s). Lines from httpx (one per AI call) are never sent. `/verbose` alone shows the level; it survives restarts.
- `/settings` lists the settings below that can change while the bot runs, with their values. `/set name` explains one, `/set name value` changes it (`/set checks 4`, `/set watch 3`, `/set poll 2-10`, `/set reply_delay 30-120`, `/set max_replies 30`, `/set dry_run on`, `/set scope all`), and `/reset name` or `/reset all` goes back to the environment's value. `/interval 2-10` still works as the old name of `/set poll`, but is no longer listed. Ranges are random between the two numbers, in the unit `/settings` shows (`m`, `s` or `h`). Changes are kept in the bot's database across restarts; a new timing also replaces the wait already going. Names: `checks`, `watch`, `poll`, `check_gap`, `reply_delay`, `active_hours`, `invites`, `accept_gap`, `catchup_gap`, `max_replies`, `max_accepts`, `backoff`, `max_backoff`, `dry_run`, `scope`.

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
