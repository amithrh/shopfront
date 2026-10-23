# AskOnce demo video script

**Hard limit: under 3:00. Target: 2:45.** English, no music, public on YouTube. The best material comes
first, because judges may stop watching at 3:00.

Spine: **ask-once hero** (0:00-1:10), then **onboarding, the contradiction catch and the verdict**
(1:10-1:58), then **the census** (1:58-2:14), then **AWS proof** (2:14-2:34), then the close (2:34-2:45).

Every feature on screen exists in the repo:

| Feature | Where it lives |
|---|---|
| Voice host, side panel, **Open owner's phone**, **New conversation**, **Present**, `/tool` | `host/voice_host.py`, `host/static/index.html` |
| Hand-off ("I don't know that. Want me to ask Rosa?") and the **Ask Rosa** card button | `server/.../tools/grounding.py` |
| Owner phone page (**Answer once**, **Save answer**, "Answer saved and on record") | `server/.../app/owner_page.py` |
| Re-certifying and publishing the owner's answer, with log line `ask-once: <business> revision N learned ...` | `server/.../app/learning.py` (test: `server/tests/test_ask_once.py`, which uses the same gift-card question) |
| "Answered by Rosa" source slip on the card | `host/static/index.html` (`urn:askonce:owner-answer:`) |
| Onboarding, quarantined injections, the Saturday contradiction | `onboarding/` (`askonce-onboard`, `out/thimbleberry/questions.json`) |
| CONDITIONAL, then CERTIFIED | `askonce-certify`, `askonce-answer`, `fixtures/owner-answers/thimbleberry.json` |
| Census numbers | `onboarding/census/census-summary.json` |
| ECS Express service `askonce-live`, log group `/askonce/mcp`, ap-southeast-2 | `infra/README.md`, `infra/askonce_infra/core_stack.py` |

**About the verdicts.** The repo's demo path is **CONDITIONAL → CERTIFIED**: a fresh onboarding has 5 open
owner questions; Rosa's answers are applied; then it is certified. NOT_CERTIFIED only happens when a check
fails, and no demo fixture fails one, so the video shows CONDITIONAL → CERTIFIED. The voiceover names
NOT_CERTIFIED in a single clause.

---

## 1. Beat table

"VO" is the voiceover, recorded separately (see section 4). "Device" lines come from the server: the
wording below is what the code produces, but Nova may phrase its own replies slightly differently. Never
put a device line in the VO.

| # | Time | On screen | VO (exact words) | Action (what to type, say or click) |
|---|---|---|---|---|
| 1 | 0:00-0:07 | Voice host in **Present** mode, idle ("Ask about a local business."). Title overlay: **AskOnce**. | "Alexa never guesses about your business. It asks you, once, and won't go live until it passes certification." | None. Hold on the idle device. |
| 2 | 0:07-0:25 | Voice host with the **Behind the scenes** panel showing. Lower third: *"Reference voice host (Alexa+ stand-in), a real MCP client. Endpoint: https://&lt;host&gt;.ecs.ap-southeast-2.on.aws/mcp"*. The panel logs `find_business` and `ask_policy`. A **Not on their website** card appears with an **Ask Rosa** button. | "This is AskOnce, one MCP server for local businesses, here behind our Alexa+ stand-in." ... *(device speaks)* ... "The website never says, so it doesn't guess." | Hold **Space** and say: **"Does Thimbleberry Bakehouse sell gift cards?"** Device: *"I don't know that. Want me to ask Rosa?"* |
| 3 | 0:25-0:37 | The panel logs `ask_owner`, its status and a task id. The card changes to **Waiting on the owner**. | "That becomes an MCP Task. The device polls it with a plain status tool, because Alexa+'s client has no Tasks extension." | Hold **Space** and say **"Yes, please."** (or tap **Ask Rosa**). Device: *"Thanks, I've passed that on to Rosa..."* |
| 4 | 0:37-0:57 | Rosa's phone page (400×840 popup, opened before the take) on the right. A new request appears within 3 s: *"Not on your website, so the assistant didn't guess..."*. Then the device: chime, purple light bar, announcement card. | "Rosa gets it on her phone, and answers once." ... "Her answer is screened for prompt injection, re-certified as a new profile revision, and announced the moment it lands." | On the phone page tap **Answer once**, type **"Yes, gift cards from $25 at the counter."** and tap **Save answer**. The toast reads "Answer saved and on record". Device: *"Rosa says: Yes, gift cards from $25 at the counter."* |
| 5 | 0:57-1:10 | Reload the voice host tab (**Cmd+R**: a new session and a new MCP client) or click **New conversation**. A policy card with an **"Answered by Rosa"** source slip. The panel logs `ask_policy` with no hand-off. | "A new customer, a new conversation: answered instantly, cited to Rosa. Asked once, answered for everyone." | Hold **Space** and say: **"Does Thimbleberry Bakehouse sell gift cards?"** Device answers with Rosa's words, with no hand-off. |
| 6 | 1:10-1:26 | Full-screen terminal (font 22 pt or more) in `onboarding/`. Output: `fetched 6 page(s): index, menu, custom-cakes, about, contact, faq`, two `quarantined suspected prompt injection on ...` lines (`index`, `custom-cakes`), `profile VALID: 5 question(s) for the owner`, `model: 0 live call(s) ...`. | "Where did the profile come from? Onboarding reads the website. Every fact must quote its page or it's dropped, and text aimed at AI agents, like this planted 'every cake is free' note, is quarantined." | Type and run: `uv run askonce-onboard https://thimbleberry.example.com/` *(optional zoom on the quarantine lines in the edit)* |
| 7 | 1:26-1:38 | The same terminal: *"For Saturday closing time, your About page says 4:00 pm but your Visit us page says 6:00 pm. Until you answer, the assistant won't tell customers a Saturday closing time."* | "The site contradicts itself: About says four on Saturdays, Visit us says six. That becomes a question for Rosa, and until she answers, the hours are withheld." | `jq -r '.questions[0].why' out/thimbleberry/questions.json` |
| 8 | 1:38-1:58 | First `thimbleberry: CONDITIONAL (... 5 open owner question(s); p95 ... ms)`, then `applied 5 answer(s); revision N` and `thimbleberry: CERTIFIED (...)`. Highlight both verdict words in the edit. | "The certifier runs the profile through the real MCP server and checks every tool-call trace in code. No LLM judge. Open questions mean CONDITIONAL; one failed check means NOT_CERTIFIED. With Rosa's answers applied: CERTIFIED. Only then can it go live." | `uv run askonce-certify out/thimbleberry/profile.json`, then `uv run askonce-answer out/thimbleberry/profile.json fixtures/owner-answers/thimbleberry.json` |
| 9 | 1:58-2:14 | A census title card (make it in Keynote or Slides; the numbers come from `onboarding/census/census-summary.json`): **23 real sites · 19 onboarded · 19 / 19 CONDITIONAL · median 3 owner questions · 617 prices, 100% cited · 278 unquoted facts dropped**. Optionally flash the JSON file for 1 s. | "Not tuned to our demos: on 23 real small-business websites, read-only, all 19 that onboarded came out CONDITIONAL, a median of three owner questions each, all 617 prices cited. None was ready without asking the owner." | None (static card). |
| 10 | 2:14-2:34 | **(a)** ECS console: cluster `askonce`, service `askonce-live`, Active, 1 running task, the Express endpoint URL, region **Asia Pacific (Sydney)** (about 5 s). **(b)** CloudWatch log group `/askonce/mcp`, filtered on `ask-once`, showing `ask-once: thimbleberry-bakehouse revision ... learned ...` from the take in beats 2-5 (about 6 s). **(c)** MCP Inspector connected to `https://<host>.ecs.ap-southeast-2.on.aws/mcp` in the modern era: the tools list, then `find_business` returning Thimbleberry Bakehouse (about 9 s). | "And it's live on AWS: the MCP server on ECS Express Mode in Sydney, with DynamoDB for profiles and tasks, and Bedrock behind onboarding and the assistant. Here's Rosa's answer being learned in CloudWatch, and MCP Inspector on the public endpoint." | Switch between the pre-opened tabs. In Inspector, call `find_business` with `{"need": "gluten-free birthday cake for 30", "locality": "Portland"}`. |
| 11 | 2:34-2:45 | End card: **AskOnce**, "Asks once. Certifies. Then goes live.", `<REPO_URL>`, `MCP: https://<host>/mcp`, and small text: "Open source: packages/mcp-tasks (SEP-2663 Tasks) · Agent Skill: askonce-onboard-business · All businesses are fictional." | "AskOnce. Alexa never guesses about your business. It asks you, once." | None. Hold the card for 4-5 s after the VO ends. |

Notes on the beats:

- **Beats 2-5 run against the AWS endpoint** (see 2.3), so the CloudWatch line in beat 10 comes from the
  take the viewer just watched. If you record the hero locally instead (2.4), beat 10(b) shows the
  `AskOnce ... ready: MCP at /mcp, storage=dynamodb, env=prod` line plus `POST /mcp` access lines.
- **Beat 6 replays recorded Bedrock replies** (`model: 0 live call(s)`). The VO says only that Bedrock is
  behind onboarding, which is true, and makes no claim that this run was live.
- **Trim the model "thinking" gaps** in beats 2, 3 and 5 to about 1 s each. Keep the device's spoken
  lines whole.

## 2. Pre-recording checklist

### 2.1 Machine and screen

- [ ] Display at 1920×1080 (System Settings > Displays, or record a 1920×1080 region). Turn on Do Not
      Disturb. Quit Slack, Mail and Messages. Hide the Dock (Cmd+Opt+D) and the desktop icons.
- [ ] **No third-party logos or branding in frame.** Run the voice host in Chrome **app mode** (no tab
      strip, no bookmarks bar). Crop the terminal and console to their content. Show no other brands.
- [ ] **Hide your AWS account ID** (top-right menu of the console) and the admin token. Blur them in the
      edit if they show up.
- [ ] **Mics.** VO: a headset or USB mic, recorded separately (section 4). Device speech recognition:
      Chrome mic permission granted for `http://localhost:8100` (do one test utterance). Use the same mic
      and speak close to it.
- [ ] **Browser zoom.** Voice host 110% (Cmd+0, then Cmd++ once; check that the side-panel text is readable
      in a 1080p frame). Owner popup 100%. AWS console and Inspector 125%.
- [ ] **Terminal.** Dark theme, font 22 pt or more, about 100 columns. Run `export PS1='$ '` and `clear`
      before each command. `jq` installed (`brew install jq`).

### 2.2 Credentials and spend

- [ ] `aws login --profile shopfront` (the voice host needs Bedrock credentials).
- [ ] Check the $5 Bedrock ledger: `PYTHONPATH=packages/askonce-bedrock/src server/.venv/bin/python -m askonce_bedrock.spend`.
      The ledger showed $3.23 spent on Oct 20. A hero take costs about 4-6 model calls, at $0.01-0.03 per
      call with Nova Pro. Reload the page between takes to keep the context short.
- [ ] Get the deployed endpoint from the `deploy.sh` output (`MCP endpoint: https://<host>/mcp`). Copy the
      admin token to the clipboard with the `aws secretsmanager get-secret-value ... | pbcopy` command in
      `infra/README.md`.

### 2.3 Start everything (Plan A: hero beat on the AWS endpoint)

From the repo root `/Users/amitmishra/worksppace-central/Mission-hackathon`:

```bash
# Terminal 1: voice host, pointed at the deployed MCP server (owner links minted with the real admin token)
ASKONCE_ADMIN_TOKEN="$(pbpaste)" AWS_PROFILE=shopfront \
  server/.venv/bin/python host/voice_host.py --mcp-url https://<host>.ecs.ap-southeast-2.on.aws/mcp
# prints: AskOnce voice host: http://localhost:8100  (MCP https://.../mcp)

# Open the device in app mode (no tab strip)
open -na "Google Chrome" --args --app=http://localhost:8100 --window-size=1500,1080 --window-position=0,0
```

In the device:

1. Check that the side panel's **MCP** stat says `2026-07-28` and **Model** shows Nova.
2. Pick **Rosa, Thimbleberry Bakehouse** and click **Open owner's phone**. Drag the 400×840 popup to the right
   edge (x ≈ 1510). It polls every 3 s, so leave it open: the request shows up during the take on its own.
3. For beats 2-5, keep the side panel visible (do not use **Present**). Use **Present** (P) only for the
   beat 1 shot.

**Before the final take, confirm the gift-card question is still unanswered on AWS.** A learned answer
persists in DynamoDB, and `seed_dynamodb.py` refuses to roll back a newer revision. Check with a tool call,
no model:

```bash
npx -y @modelcontextprotocol/inspector@2.8.0 --cli https://<host>/mcp --transport http --protocol-era modern \
  --method tools/call --tool-name ask_policy \
  --tool-args-json '{"business_id":"thimbleberry-bakehouse","question":"Do you sell gift cards?"}' --format json
```

Expect `"facts": []` and a `handoff`. If it is already answered, use a backup question that you have first
checked the same way and seen hand off (for example "Do you have a loyalty card?" or "Is there Wi-Fi?"),
and change the owner's answer to match.

**Never rehearse beats 2-5 against AWS.** Rehearse locally (2.4), where a restart resets everything. The
AWS take is one shot per question.

### 2.4 Local server (rehearsals, and Plan B if AWS misbehaves)

```bash
# Terminal 1: server, in-memory storage (a restart clears every task and every learned answer)
cd server && ASKONCE_PROFILE_DIRS=../profiles/live \
  ASKONCE_CORS_ORIGINS=http://localhost:6274,http://localhost:5173 uv run python -m askonce_server

# Terminal 2: voice host on the local server (dev admin token by default)
AWS_PROFILE=shopfront server/.venv/bin/python host/voice_host.py
```

$0 rehearsal with no model: type these into the device's text box. The panel shows "Rehearsal: tool called
directly, no model", so keep that out of the final take.

```
/tool ask_policy {"business_id":"thimbleberry-bakehouse","question":"Do you sell gift cards?"}
/tool ask_owner {"business_id":"thimbleberry-bakehouse","question":"Do you sell gift cards?"}
```

To reset: stop the server (Ctrl+C), start it again, then reload the device. Re-open the owner's phone
after a server restart, because the old page keeps polling the old server.

### 2.5 Onboarding terminal (beats 6-8)

```bash
cd /Users/amitmishra/worksppace-central/Mission-hackathon/onboarding
git status --short out/            # should be clean
# beat 6
uv run askonce-onboard https://thimbleberry.example.com/
# beat 7
jq -r '.questions[0].why' out/thimbleberry/questions.json
# beat 8
uv run askonce-certify out/thimbleberry/profile.json
uv run askonce-answer out/thimbleberry/profile.json fixtures/owner-answers/thimbleberry.json
```

- Replay mode (no `--live`) makes **no Bedrock calls**. `askonce-answer` rewrites
  `out/thimbleberry/profile.json`, and the certifier rewrites `certification-verdict.json`.
- **Reset between takes** with `git restore out/`, then `git status --short out/`. Don't commit the
  rewritten files.
- Do a dry run once to see the exact counts. Don't narrate any number from this beat: the VO only names
  the verdict words.

### 2.6 AWS proof tabs (beat 10), opened in a normal Chrome window before recording

1. **ECS** console (Region: Asia Pacific (Sydney)) > Clusters > `askonce` > Services > `askonce-live`.
2. **CloudWatch** > Log groups > `/askonce/mcp` > Search all log streams, filter `"ask-once"`. Refresh
   after the hero take. Optionally start **Live Tail** before the take.
3. **MCP Inspector**, web UI, on the public endpoint:

```bash
npx -y @modelcontextprotocol/inspector@2.8.0 --web --transport http \
  --server-url https://<host>.ecs.ap-southeast-2.on.aws/mcp --protocol-era modern
```

**Test this before recording.** The deployed server allows only its own origin (`ASKONCE_ALLOWED_ORIGINS`
and `ASKONCE_CORS_ORIGINS` are set to the service URL in `core_stack.py`), so the Inspector web UI at
`http://localhost:6274` may get a 403 or a CORS error from the browser. If it does, use the **Inspector
CLI** on camera instead. It runs in Node and sends no browser Origin:

```bash
npx -y @modelcontextprotocol/inspector@2.8.0 --cli https://<host>/mcp --transport http --protocol-era modern \
  --method tools/call --tool-name find_business \
  --tool-args-json '{"need":"gluten-free birthday cake for 30","locality":"Portland"}' --format json
```

Beat 10's VO works for either view.

### 2.7 Window layout

| Beats | Layout |
|---|---|
| 1 | Voice host app window, Present mode, scaled to full frame in the edit |
| 2-5 | Voice host 1500×1080 at the left, owner popup 400×840 at the right |
| 6-8 | Terminal, full screen |
| 9 | Census card, full screen |
| 10 | Chrome (ECS, CloudWatch, Inspector tabs), full screen, 125% |
| 11 | End card |

## 3. Voiceover word count

Speaking rate: about 150 words per minute (2.5 words per second).

| Beat | Slot | VO words | VO time | Rest of the slot |
|---|---|---|---|---|
| 1 | 7 s | 18 | 7 s | none |
| 2 | 18 s | 22 | 9 s | the customer's question and the device's reply (about 7 s) |
| 3 | 12 s | 21 | 8 s | the device's reply |
| 4 | 20 s | 28 | 11 s | typing (under the VO) and the announcement (about 4 s) |
| 5 | 13 s | 16 | 6 s | the question and the answer |
| 6 | 16 s | 35 | 14 s | none |
| 7 | 12 s | 27 | 11 s | none |
| 8 | 20 s | 40 | 16 s | the command output |
| 9 | 16 s | 36 | 14 s | none |
| 10 | 20 s | 41 | 16 s | the tab switches |
| 11 | 11 s | 11 | 5 s | end card hold |
| **Total** | **2:45** | **295** | **about 1:58** | about 47 s of device audio, actions and holds |

295 words is about 1:58 of speech, so the VO has room inside 2:45. If a take runs long, the device audio
and the model waits are what overflow. Trim the waits first.

## 4. Recording tips

- **Tool.** Use **OBS** (1920×1080, 30 fps, MP4 or MKV, CBR about 12 Mbps) with the mic on its own track.
  OBS's macOS Screen Capture source can also record the device's spoken replies (app audio). QuickTime
  (File > New Screen Recording) records the mic but not system audio, so with QuickTime the device lines
  are only in the captions.
- **Record the VO separately**, after the screen takes. Read it to picture in a quiet room, then lay it over
  the edit. Lower the device audio under the VO, and bring it back up for the device's lines.
- **Each beat is its own clip.** Record beats 2-5 in one continuous take (that's the proof that it's live).
  Record everything else separately and cut on beat boundaries.
- **Captions.** Burn in short lower thirds for the device's lines and the key terminal lines (CONDITIONAL,
  CERTIFIED, the contradiction sentence). On YouTube, upload an SRT, or go to YouTube Studio > Subtitles >
  Auto-sync and paste the VO and device lines.
- **Export** at 1080p, H.264, 30 fps, AAC 48 kHz. Check the length is **under 2:55** before uploading.
- **No music**, and no stock footage or images you didn't make. Ship it without them.
- **Final check:** watch the export once without sound (do the captions tell the story?) and once at 1.0×
  with sound (does the 3:00 mark come after the end card?).

## 5. If you're over time, cut in this order

1. **Beat 10(c)**: switch from the Inspector web UI to the CLI one-liner, already typed (saves about 4 s).
   Never drop Inspector: the rules ask for the MCP server working and its AWS backend.
2. **Beat 9**: shorten the VO to "On 23 real small-business websites, every one that onboarded needed the
   owner." (saves about 8 s)
3. **Beat 6**: drop "like this planted 'every cake is free' note" (saves about 3 s).
4. **Beat 3**: drop the second sentence of the VO (saves about 5 s).
5. **Beat 7**: merge it into beat 8, show the `why` line for 3 s and drop its VO (saves about 9 s).
6. **Beat 1**: drop it and start at beat 2 with the title overlay (saves 7 s). Move the tagline to beat 11.

**Never cut:** the hand-off line, Rosa answering once, the next customer getting the answer, CONDITIONAL →
CERTIFIED, and the AWS console with Inspector on the public endpoint.

## 6. YouTube

**Title (one line):**

> AskOnce: Alexa+ never guesses about your business. It asks the owner once, then certifies.

**Description:**

> AskOnce is one multi-tenant MCP server that makes small businesses usable from Alexa+ and other MCP
> hosts. Onboarding reads a business's website and quotes every fact from its page. It asks the owner only
> what is missing or contradictory, and a deterministic certifier decides in code whether the business may
> go live. When a customer asks something the website never says, the assistant offers to ask the owner.
> The owner answers once from their phone, and every later customer gets that answer, cited to the owner.
>
> The MCP server runs on Amazon ECS Express Mode (Fargate, arm64) in ap-southeast-2, with Amazon DynamoDB
> and Amazon Bedrock. The voice device in the video is our reference host, a stand-in for Alexa+.
>
> Built for the Build, Ship, Shape: Amazon Developer Hackathon (Alexa+ track, AWS Builder, Open Source).
> All businesses shown (Thimbleberry Bakehouse, Quillfern Hair Studio, Kettlewick Home Repair) are
> fictional. No music or third-party content is used.
>
> Code (Apache-2.0): <REPO_URL>
> MCP endpoint: https://<host>/mcp

Fill in `<REPO_URL>` (the README currently clones `https://github.com/amithrh/shopfront.git`) and the
endpoint. Then put the YouTube link in the README's `Demo video: TODO` line.
