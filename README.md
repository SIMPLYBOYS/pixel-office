# Pixel Office

**English** | [繁體中文](README.zh-TW.md)

Pixel Office projects the work of AI agents into a pixel-art office. One glance tells you who is doing
what, who is stuck, and what is waiting for your approval. Every employee has a persona, a schedule,
a work thread and an audit ledger. High-risk operations stop and wait for you to approve them
(human-in-the-loop). Every animation and badge maps to a real event: **what you see on screen
actually happened**.

Three engines can be chosen per task: cogito-agent, Claude Code CLI and Codex CLI.
The stage runs in Unity, the bridge runs on FastAPI, and the two exchange JSON commands over WebSocket.

## Works with cogito-agent

[cogito-agent](https://github.com/SIMPLYBOYS/cogito-agent) is the engine half: a self-hosted Go ReAct agent (Claude-first,
Slack/Telegram, named subagents, HITL approvals, cost circuit breakers). Pixel Office is the stage half: it turns cogito's events
into employees you can watch, and approvals you can sign. Set `COGITO_OFFICE_URL=http://localhost:8123` on the cogito side; on the
same machine it sends the bridge token automatically. The event protocol is specified in cogito's
[docs/office-protocol.md](https://github.com/SIMPLYBOYS/cogito-agent/blob/main/docs/office-protocol.md).
The Claude Code and Codex engines work without cogito.

## Video tour

<a href="docs/media/pixel-office-demo.mp4"><img src="docs/media/pixel-office-demo.gif" width="300" alt="Pixel Office demo video"></a>

A 26-second vertical demo (the GIF above is silent; click it for the mp4 with sound effects). The office,
the roster and the task board are screen recordings and screenshots of the running app, not animation.
The two segments marked "recreated" were rebuilt in the shell's own style, because there was no live
collaboration board or pending approval to record at the time.

The UI is in Traditional Chinese; on-screen text is quoted below with a translation.

| Time | On screen | Feature |
|------|-----------|---------|
| 0:03 | Employees get up and sit down in the meeting room | `/cmd` movement; six meeting-room seats (`meet_*` in `RoomBuilder.cs`) |
| 0:06 | Four people start work at the same time | The 09:00 schedule kicks off several agents in parallel; the roster's 「工作中」 (working) status lights |
| 0:10 | Every task in the office on one board | The shell's 「📋 任務」 (Tasks) tab: four columns for in progress / awaiting approval / done / failed or stopped |
| 0:14 | One big job, the whole team works through its dependencies (recreated) | The roster's 「看板」 (Kanban) collaboration mode: split into tickets, mark 「⏳ 等」 (waiting on) whom, at most 3 in parallel |
| 0:19 | High-risk operations stop and wait for your go-ahead (recreated) | HITL approval card plus the countdown badge over the employee's head (the badge is the real in-office sprite) |
| 0:23 | Three engines, switchable per task | cogito-agent / Claude Code / Codex CLI |

(Formerly `unity_demo`. Architecture details live in the architecture guide and build notes in aaron-vault.)

```
unity/      Unity 6 project (presentation: scene, NPCs, pathfinding, speech bubbles)
backend/    FastAPI bridge (personas + memory + dispatch + approvals + schedule + audit ledger)
web/        Web shell (roster, work threads, task board, command page)
tools/      Asset pipeline (aseprite design breakdown, character frame extraction)
limezu/     LimeZu licensed assets and derivatives (not in git; see "Rebuilding assets" below)
```

## Daily startup (two windows)

**Window 1: the backend brain**

```bash
cd backend
.venv/bin/uvicorn main:app --port 8123
```

The mode comes from `OFFICE_MODE` in `backend/.env` (loaded automatically by `load_dotenv`):
- `OFFICE_MODE=projection` (recommended default): the life-simulation brain (Claude deciding when to
  wander and chat) is off. NPCs idle at zero cost (with the occasional random walk), and only cogito's
  `/office/event` work events drive behavior. Use this for real work: it spends no API credits, and the
  status display isn't polluted by wandering.
- Comment the line out for the life-simulation demo mode (Claude decides how everyone spends the day).
- How to tell it took effect: once Unity connects, the log prints 「啟動 N 個 agent（純投影…）」
  ("starting N agents (projection only…)"). Restart uvicorn after changing `.env`.

**Team and personas**: the office has 8 fixed workstations (`backend/slots.yaml`: look, seat, pose); the team setup decides
which are active and who sits there. The first time you open the shell you get the **team setup wizard** (pick a template →
pick workstations → set up each person); later, adjust it from 「👥 團隊設定」 (team setup) on the roster.
The result lives in `backend/team/` (not in git): one `pXX.yaml` (name / role / team / personality…) and one `pXX.md`
(character sheet) per workstation. Templates are in `backend/templates/` (demo team, software team). Spec:
[`docs/team-setup.md`](docs/team-setup.md). A 9th person needs a new character and seat in Unity; the web can't add one.

When `COGITO_CHANNELS=<cogito>/workspace/channels` is set, the bridge syncs each `pXX.md` on startup into
that channel's `AGENTS.md` **and** `CLAUDE.md`, with identical content: cogito's PromptComposer reads the
former and Claude Code (the CLI engine) reads the latter, which is how the persona actually shapes behavior.
The CLI **does not read AGENTS.md** (tested 2026-09-07), so both filenames are required.
⚠️ Overwrite protection: a file is only written if it doesn't exist or starts with the
`<!-- office-persona:` marker. Hand-written files are always kept and a warning is printed. To maintain a
channel's file yourself, delete those two marker lines.

**Schedule and delivery**: `backend/team/schedule.json` (not in git) holds the office's recurring jobs (format in
`schedule.json.example`): `hour` is required, omitting `weekday` means every day, `engine` picks cogito or cli
per job, and `repo` binds a working repo. When a job is due it goes through the normal dispatch path, so
movement, work thread and report card all behave as usual. A job with `"deliver": {"file": "trend-{date}.md"}`
sends that file to `OFFICE_DELIVER_TO` when it finishes (`telegram:<chat_id>,slack:<channel_id>`, the same
format as cogito's `COGITO_CRON_NOTIFY`), so the boss sees it even when away from the office. A missing file,
a file that wasn't updated or an API error is stated plainly in the work thread; it only says
「已送到」 ("delivered") once delivery actually succeeded.

Office-wide rules live in one source, `backend/personas/office.md`, which is synced on startup into each
engine's "everyone" slot: cogito's shared root `workspace/AGENTS.md` (PromptComposer reads the root first,
then layers the channel's file on top) and the employee CLI profile's `$CLAUDE_CONFIG_DIR/CLAUDE.md`
(Claude Code's user-level instructions, confirmed to load). Same overwrite protection.

Speech-bubble CJK font (one-time): `tools/get_font.sh` fetches Noto Sans CJK TC (OFL license, 16MB,
gitignored) into `unity/Assets/Resources/OfficeFont.otf`. On import the editor bakes only the characters used
in bubbles into an atlas (see the character list in `OfficeFontImporter.cs`), so the font doesn't bloat the
build. Without this file, Chinese bubbles are blank in WebGL (Unity's built-in Arial has no CJK glyphs).
**When you change bubble wording, update that character list too.**

Roster avatars (optional, one-time): `python3 tools/make_avatars.py` extracts 64×64 pixel avatars from the
character sheets into `backend/avatars/` (LimeZu derivatives, gitignored; without them the roster shows text
avatars). Grouping follows each persona's `team` field; people without one go under 「未分組」 (ungrouped).

Reading reports: in Play mode, **click any NPC** to open the report card for their latest task (task, status,
full report; Esc or click elsewhere to close). The data comes from the bridge's `GET /office/report/{id}`;
for artifacts, open claw-dashboard.

## Web version (web shell, Pixffice-style layout)

1. Unity menu **Tools → Build WebGL (辦公室網頁版)** (once; rebuild only when the scene changes; output
   goes to `unity/Builds/WebGL`, gitignored)
2. Start the backend and open the 「🔑 外殼網址」 (shell URL) it prints (`http://127.0.0.1:8123/shell/#t=…`; needed once per
   browser, after that plain `/shell/` works): employee roster with status lights on
   the left, the pixel office (WebGL) in the middle, the work thread on the right (live timeline plus full
   reports)
3. Works without a WebGL build too: the middle shows a hint, and the roster and work threads keep working
   (same data from `/agents` and `/office/report`)

Where output files go: each channel has its own working directory (cogito's
`workspace/channels/<platform>_<channel>`); dispatching to 阿哲 from the web means `office_p17`. Task cards
show 「📁 channels/office_p17」 (hover for the full path).

**Dispatching from the web**: click an employee on the left → describe the task in the box below →
Ctrl+Enter to hand it over. This requires cogito's bot to have its HTTP entry point on (cogito `.env`:
`COGITO_HTTP_ADDR` + `COGITO_HTTP_TOKEN`, with `office-web` listed in `COGITO_ALLOWED_USERS`; bridge `.env`:
`COGITO_HTTP` + the same token). High-risk operations pop up an **approval card** in the right column
(approve / reject buttons), auto-rejected on timeout. The roster doesn't depend on Unity: you can dispatch
and watch progress without Unity open. Unity is only the rendering surface.

Two ways to dispatch (usable together):
- **One-off (CLI)**: `claw-cli -office http://localhost:8123 -office-agent p17 -dir . -prompt "..."`
- **Always-on (Slack/Telegram bot)**: start the cogito bot with `COGITO_OFFICE_URL=http://localhost:8123`
  and tasks from its channels are projected automatically. Unknown channel ids get an idle NPC assigned by
  the bridge (sticky: the same channel always maps to the same employee).

The bridge **only accepts local requests**: a Host other than localhost / 127.0.0.1 gets a 403, and other websites
can't get in either (cross-site requests and `/ws` are both blocked; audit #4). To open the shell from another device on
your LAN, set `OFFICE_ALLOWED_HOSTS` in `.env`.
On top of that, a **token** is required. It lives in `~/.pixel-office/token` (readable only by you; created on first start and kept
after that). The shell trades the printed URL for a cookie once; the hooks and cogito read the file automatically; hand-typed curl
must send it (see "Observing and intervening"). Only static files, `/ws` (Unity) and `GET /office/report` (Unity's report card)
work without it. If the token leaks, delete that file and restart the bridge to get a new one (log in to the shell again with the new URL).

If startup prints 「⚠ 未設定 ANTHROPIC_API_KEY」 ("ANTHROPIC_API_KEY not set"), `.env` wasn't loaded
(see one-time setup below). NPCs still move in this mode, but only as random walks.

**Window 2: Unity**

1. Open the project `unity/` in Unity Hub (not the repo root!)
2. Open the scene `Assets/Scenes/SampleScene`
3. Press **Play**

Either can start first (Unity retries the connection every 3 seconds). How to tell they're connected:
- Unity Console: 「BrainGateway: 已連上後端，假大腦停用」 ("connected to backend, fake brain disabled")
- Backend terminal: 「✓ Unity 已連線」 ("Unity connected") → 「啟動 3 個 agent（Claude 決策）」
  ("starting 3 agents (Claude deciding)")

Then enjoy the show: the backend terminal scrolls each person's decisions; lines starting with `💬` are
conversation loops.

## One-time setup (new machine)

1. **Unity 6000.5.3f1** (Unity Hub → Add project from disk → pick `unity/`).
   The first open rebuilds the Library cache and fetches the NativeWebSocket package (needs network and
   git); let it finish.
2. **Backend environment**:
   ```bash
   cd backend
   uv venv .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   cp .env.example .env   # fill in ANTHROPIC_API_KEY=sk-ant-...
   ```
3. **Rebuilding assets** (fresh clones only; `limezu/` is not in version control):
   - Buy LimeZu's *Modern Interiors* (full version) and *Modern Office* on itch.io and unzip them into `limezu/`
   - **Asset license and credit**: pixel art © [LimeZu](https://limezu.itch.io) (Modern Interiors / Modern
     Office). The license allows use in any project but **forbids redistributing the assets themselves**,
     so `limezu/` and `unity/Assets/Sprites/LimeZu/` are never committed. A fresh clone without them can
     still run the bridge and the shell (the office view needs you to buy and regenerate the assets).
     Modern Interiors requires the credit: limezu.itch.io
   - Run the pipeline to regenerate the assets and copy the output into Unity:
     ```bash
     python3 tools/extract_design.py limezu/Modern_Office_Revamped_v1.2/6_Office_Designs/Office_Design_2.aseprite
     for n in 17 1 7 5 12 8 19; do python3 tools/make_character.py $n; done   # seven employees (incl. one-shot action rows)
     python3 tools/make_emotes.py                                              # status badges beside the head
     # output is in limezu/_extracted/; copy into unity/Assets/Sprites/LimeZu/
     # (Design/, Characters/p*/, Emotes/)
     python3 tools/trim_props.py    # ⚠ run AFTER copying: it edits Design/ under Unity directly
     ```
   - `trim_props.py` shortens the long lower desk `obj_24`. The extractor splits components by connected
     pixels, so it merged the 4th workstation's desktop and the corner plant into the long desk's image.
     When the rightmost workstation is removed (`Hidden` in RoomBuilder), the desk has to shrink with it or
     it leaves an unfinished cut. **Skipping this step = the right aisle gets blocked by the regrown desk.**
   - Unity menu **Tools → Build Room** and **Tools → Build Characters** to rebuild the scene

## Observing and intervening

```bash
# the bridge needs the token (audit #4): read it once, send it on every call
T=$(cat ~/.pixel-office/token)
curl -H "X-Office-Token: $T" localhost:8123/agents     # everyone's position, memory, whether they're chatting
curl -H "X-Office-Token: $T" localhost:8123/events     # recent events (arrived, etc.)
# bypass the brain and issue a command directly:
curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' -H "X-Office-Token: $T" \
     -d '{"agent_id":"p17","action":"move_to","target":"cooler_1"}'
# strike a pose directly (the fastest way to check an animation, no need to wait for a real agent run):
curl -X POST localhost:8123/cmd -H 'Content-Type: application/json' -H "X-Office-Token: $T" \
     -d '{"agent_id":"p05","action":"use","target":"hurt_down"}'
```

Pose names: `sit_up` / `sit_left` / `sit_right`, `phone`, `sleep`, `book`,
`face_*`, `gift_*`, `hurt_*` (`pick_up_*` / `lift_*` / `throw_*` are in the prefab but not wired to events yet).
`hurt_*` is a one-shot action: **it plays for a second and reverts on its own**, easy to miss; to capture it
in a screenshot, keep resending the command.

Badges beside the head (so you can tell at a glance who is blocked). Only one shows at a time, and
**top to bottom is the priority order**: while a higher one applies, the lower ones don't show.

| Badge | Meaning | How it clears |
|-------|---------|---------------|
| Countdown pie (green → yellow → orange → red) | Waiting for you to approve / reject; the fuller the pie, the closer to auto-reject on timeout | Approve or reject in the shell |
| Blue question mark | Same, but the fallback when **no deadline can be computed** (no countdown is drawn if the time left is unknown) | Same |
| Red exclamation mark | Blocked by the subscription quota | Wait for the quota to recover; clears on the next tool event |
| Yellow exclamation mark | Quota nearly full (over 90%) | Clears automatically when the task ends |
| Empty thought bubble | Stuck spinning; the person has walked off to the water cooler | Clears on the next tool event |
| Yellow gem | Pending memory proposals (things they learned that nobody has reviewed) | Expand the 💡 list on the roster and approve or discard each one |

The two quota badges currently **only light up for the CLI engine**: cogito retries 429s at the provider
layer and swallows them, so they never reach the office protocol.

The same state also appears on the roster rows on the left (from the same `want_emote()`). The roster adds
two things: the Kanban agent has no body and can't wear a badge, so those 33 proposals are only visible in
the roster; and **the roster keeps working without a WebGL build**. Proposal counts are shown
**separately** in the roster: a head can only wear one thing at a time, but the roster has room side by side,
so "waiting for approval" and "6 proposals nobody has reviewed" are both visible at once.

Three run modes:
| Mode | Condition | Behavior |
|------|-----------|----------|
| Real brain | Backend on + API key | Claude decides, persona dialogue, conversation loops |
| Contract test | Backend on, no key | Random walks (for verifying the pipeline) |
| Offline | Backend off | Unity's fake brain wanders on its own + 「...」 bubbles on encounters |

The backend can be turned on and off at any time; the fake and real brains hot-swap automatically.

## Common tuning

- Pace: `DECISION_INTERVAL` in `backend/main.py` (decision interval)
- Cost: `MODEL` in `backend/agent.py` (switching to `claude-haiku-4-5` saves 80%)
- Personas: 「👥 團隊設定」 in the shell, or edit `backend/team/*.yaml` directly (direct edits need a backend restart)
- Conversation cap: `MAX_ROUNDS` in `main.py`

## Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| `address already in use` | An old uvicorn holds port 8123; Ctrl+C it or find it with `lsof -i :8123` |
| Unity never connects | The scene needs a `BrainGateway` object (if missing, run Tools → Build Characters); it only connects in Play mode |
| All NPCs stand still | Check the backend log for 429 / decide failures; in no-key mode check that random walks are printed |
| Compile errors | The real error message is in `unity/Logs/Editor.log` (not the one under ~/Library) |
| Hub opened the wrong folder | The project is `unity/`; opening the repo root creates an empty project (gitignore guards against it) |

## License

The code is MIT (see `LICENSE`) and **covers only the code written for Pixel Office**.

Pixel art © [LimeZu](https://limezu.itch.io) (Modern Interiors / Modern Office).
The assets are **not included in this repository or in any release**, and MIT cannot relicense them.
To run the office view, get a legitimate copy from itch.io and regenerate the assets as described in
"Rebuilding assets" above. The Modern Interiors license requires the credit: limezu.itch.io.

The demo video (mp4 and GIF) in `docs/media/` is a recording and screenshots of the real office screen, so
LimeZu art is visible in it. It is only for showing this project. It is not an asset file, the original tiles
can't be recovered from it, and it is **not covered by the MIT license**.

Third-party sources and license boundaries are listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)
(written in Traditional Chinese).
