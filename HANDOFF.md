# MY-ror smart mirror: project handoff

Last updated 2026-11-25. This file is self-contained: a new session should be able to start from it without the earlier conversation.

**Status: planning is done, nothing is built.** There is no application code, no git repo and no hardware assembled. The only code here is a 3D visual model of the mirror.

**Where this file and the spec doc disagree, this file wins.** Section 4 lists what the spec gets wrong.

Labels used throughout:

| Label | Meaning |
| --- | --- |
| **[USER]** | The user decided this. Don't change it without asking. |
| **[PROPOSED]** | Claude proposed it. The user has not objected, but has not explicitly confirmed it either. |
| **[VERIFIED]** | Checked against a source (section 22). |
| **[UNTESTED]** | An assumption or estimate. Measure it before relying on it. |

---

## 1. What we are building

A bathroom smart mirror that shows the user's day and recommends outfits from clothes they own. It runs on a Raspberry Pi 4 (4 GB) and extends [MagicMirror²](https://github.com/MagicMirrorOrg/MagicMirror). There is one user, so scale is out of scope.

The original vision, and where each part stands:

| Original idea | Status |
| --- | --- |
| Weather, to-dos, calendar on the mirror | MVP (M0–M1) |
| Phone dashboard to control the mirror | MVP, as a web app |
| Camera on the mirror, triggered from the phone | MVP (M4) |
| Segment and detect clothes with a vision model | MVP (M4 check-ins) |
| Smart closet that learns what they own, with a UI | MVP (M2) |
| "Generate an outfit for me today" | MVP (M3) |
| Learn the user's style over time | MVP: quiz, feedback and an editable profile |
| "I want to look like this style / person" | **Deferred to v2 [PROPOSED]. Core to the original vision, so confirm.** |
| Show a picture of what to wear | MVP: collage of real items. Virtual try-on is v2 [PROPOSED] |
| Fit the user's body proportions | **Unresolved.** Claude advised self-reported fit notes over photo estimates; the user never answered |
| Photos in S3 | Replaced by a local SSD plus encrypted R2 backup [PROPOSED] |

**MVP goal:** add 30+ real items in one afternoon. Then, on a normal morning, walk up and see a sensible outfit within 2 seconds.

**Success criteria [PROPOSED]**

- The outfit is computed ahead of time and appears within 2 s of presence detection.
- Zero invented items: every suggested ID exists in the closet.
- API spend stays under the cap, enforced in code.
- Once assembled, the mirror should not need repairs. **[USER]** Anything that could still fail is reachable from the back panel.

---

## 2. What exists today

| Thing | Location | State |
| --- | --- | --- |
| This handoff | `HANDOFF.md` | Current |
| 3D hardware model, source | `mirror-model.html` | HTML fragment, no doctype |
| 3D hardware model, hosted copy | `deploy/index.html` | Generated from the fragment |
| Live 3D model | https://my-ror-mirror-model.vercel.app | **Public.** Returned 200 and was byte-identical to `deploy/index.html` on 2026-11-25 [VERIFIED] |
| Spec document | https://claude.ai/artifact/1etvGzXBmkVzHXNfZyvFgt | Unedited since it was written. Its one open comment (check-ins) is answered by U22 |
| claude.ai copy of the 3D model | claude.ai/artifact/V8yTEVzKLi2VbzzSWyYZVm | Gone (reported "not found") |

Hosting: Vercel project `my-ror-mirror-model`. Share the short URL; the longer team URL sits behind Vercel login.

The model's size, cover slider, camera position and French cleat are illustrative, and its screen shows sample data. In-browser checks covered loading and rendering only. The capture demo, exploded view and phone layout were not tested.

Rebuild the hosted copy after editing the model (run from the project root):

```bash
{ printf '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n<style>body{margin:0}</style>\n'; sed -n '1,/^<\/style>$/p' mirror-model.html; printf '</head>\n<body>\n'; sed -n '/^<\/style>$/,$p' mirror-model.html | tail -n +2; printf '</body>\n</html>\n'; } > deploy/index.html
```

---

## 3. Decision log

### Decided by the user [USER]

| # | Decision |
| --- | --- |
| U1 | Raspberry Pi 4 with 4 GB RAM; build on MagicMirror² |
| U2 | Mounted in the bathroom, above the sink |
| U3 | Control is a web app |
| U4 | Used at home only: no Tailscale, no public URL |
| U5 | Segmentation uses a hosted model in the final product. SAM 3 was a starting idea, not a requirement |
| U6 | Development and pipeline iteration run on local models on their RTX 3080; paid APIs only for final checks. The final product uses cloud APIs |
| U7 | API budget under $5 (period not stated; treated as monthly) |
| U8 | At most about 300 photos a year |
| U9 | LED strip around the mirror |
| U10 | Capture uses a countdown and a full-white screen flash |
| U11 | Detect when the mirror moves rooms or houses |
| U12 | LD2410 presence sensor |
| U13 | Manual lens cover with a small lever, "inches max". A servo was rejected as too hard to repair |
| U14 | Hall sensor on the cover |
| U15 | To-dos from iPhone Reminders |
| U16 | Calendars from Google, iCloud and Outlook |
| U17 | Bulk closet onboarding with the phone camera |
| U18 | 24" monitor |
| U19 | Style boards (user-supplied images) instead of scraping Instagram or Pinterest |
| U20 | The mirror is two-way glass, already owned (size and transmission not yet given) |
| U21 | The 24" monitor is already owned (model not yet given) |
| U22 | Mirror check-ins stay in the MVP |
| U23 | Plain HTTP on home Wi-Fi is acceptable, knowing photos and tokens travel unencrypted |
| U24 | Face-blurred mirror photos may be sent to fal and Claude for check-ins. This does not cover unblurred photos or other services |

### Proposed, not explicitly confirmed [PROPOSED]

| # | Proposal | Note |
| --- | --- | --- |
| P1 | Backend and data live on the Pi | Implied by U4, never stated outright |
| P2 | Python (FastAPI) backend, Node for MagicMirror, React (Vite) phone app | |
| P3 | Claude Haiku 5.5 for tagging, Claude Sonnet 5.5 for outfits | Chosen by Claude for cost. Opus 5.5 also fits the cap (section 15) |
| P5 | Review inbox: nothing enters the closet without confirmation | |
| P6 | Nightly encrypted backup to Cloudflare R2 | |
| P7 | Demister pad behind the glass at the camera | |
| P8 | French cleat wall mount | |
| P9 | Outfit rules and thresholds (section 10) | Starting values |
| P10 | In development, use the Claude API for LLM calls and the 3080 for GPU models, with a separate development spend cap | The user had no preference when asked. Tagging a 50-photo golden set costs about $0.02, and one outfit generation about $0.025 [UNTESTED]. Set `LLM=claude` by default, and keep the switch so local models can be tried |

---

## 4. Corrections and conflicts

Earlier answers, the spec doc or the 3D model say these things. They are wrong, unproven or contradictory.

### Safety and purchases (read before buying)

| # | Earlier claim | Correction |
| --- | --- | --- |
| C1 | Put a power strip or outlet cube inside the frame | **Don't.** Mains wiring in a closed wooden frame in a bathroom is a shock and fire risk. Prefer power supplies outside the frame with only low-voltage DC entering it. The monitor's supply needs its own plan. Have a qualified electrician review it; local code applies |
| C2 | AOD4184 and IRLZ44N modules suit the Pi's 3.3 V pins | Their on-resistance is specified from 4.5 V (AOD4184A) and 4 V (IRLZ44) [VERIFIED]. Use a part specified at 2.5 V, such as AO3400A (30 V), or a 5 V gate driver. Size it for the strip's current |
| C3 | The DRV5032 is only a tiny surface-mount chip, so buy a breakout | It also comes in a through-hole **TO-92** package [VERIFIED], which is easier to solder. Check the pinout for the exact part, since TO-92 pin order varies |
| C4 | "DRV5032, omnipolar" is enough to order | Sensitivity varies by suffix. FA, FB and FC are omnipolar with a 4.8 mT max operate point. AJ is 9.5 mT (too marginal) and ZE is 63 mT (won't trigger). FD is unipolar [VERIFIED]. Order **FB** or **FC** in TO-92; confirm FA and stock in the datasheet |
| C5 | The stock camera ribbon is too short | Unproven. Listings say 150 or 200 mm. Measure the real Pi-to-camera distance first |
| C6 | Look for "UASP support" on the SSD | Not sufficient. Some USB-SATA adapters misbehave on the Pi 4. Choose one known to boot a Pi 4 |

### Design

| # | Earlier claim | Correction |
| --- | --- | --- |
| C7 | LEDs run at "a fixed level" during capture | Use **100% duty** (no PWM) while capturing. PWM dimming can cause banding with a rolling-shutter camera |
| C8 | "Camera powered down when idle" | Software can close the sensor, but that isn't a hardware power cut. The physical cover is the real protection |
| C9 | The hall sensor reports open or closed | It only detects **closed**. A half-open cover also reads "open", so the black/occluded-frame check is still required |
| C10 | Onboarding upload "survives the phone going to sleep" | Only server-side processing does. iOS Safari may stop an upload when locked. Upload one file per request and make it resumable |
| C11 | Photos never leave the house whole | **Conflicts with the design.** Hosted background removal receives the full phone photo, and check-ins send a full face-blurred frame. See section 14 |
| C12 | Calendar feeds go straight into MagicMirror | That would not deduplicate. The backend should merge the feeds and serve one to MagicMirror |
| C13 | Milestones end at M4 | No step covered final assembly and mounting. Added as M5 |
| C14 | "Never open the mirror" and "reachable from the back panel" | Read as: no expected maintenance, and serviceable if something does fail |

### AI, cost and development

| # | Earlier claim | Correction |
| --- | --- | --- |
| C15 | The spec uses the Claude API in development | **Conflicts with U6.** Claude recommended it because prompts tuned on another model may not transfer, and the cost is small. Asked on 2026-11-25, the user had no preference, so P10 is the default |
| C16 | The 3080 can run SAM 3 | Unproven. Third-party reports for image inference range from about 8 to 12 GB, and one setup guide recommends 16 GB. The 3080 has 10 or 12 GB. Weights are gated and some access requests are refused |
| C17 | One fal request covers every garment box | The schema accepts several boxes, but behavior is [UNTESTED]. Budget for one call per garment until proven |
| C18 | Caching the closet list cuts outfit cost | Little benefit. Cache entries are short-lived and this app makes a couple of calls a day. It may help only quick rerolls |
| C19 | SAM 3 is the latest hosted option | fal also lists SAM 3.1 at `fal-ai/sam-3-1/image`, $0.01 per request [VERIFIED]. Include it in the bake-off |

---

## 5. Open questions for the user

| # | Question | Blocks |
| --- | --- | --- |
| Q3 | Exact size, thickness and transmission of the two-way glass? Is it bigger than the monitor, with room for the camera? | All geometry |
| Q4 | Monitor model, VESA mount, port location, thickness? | M0 |
| Q5 | Which 3080: 10 GB or 12 GB? OS? Can it use Wake-on-LAN? | Dev setup |
| Q6 | Frame material: wood, plastic or metal? | Cover mechanism |
| Q7 | Camera top center or offset? Which edge gets the lever? | Cover mechanism |
| Q8 | Is $5 monthly, and does it exclude development? | Budget cap |
| Q11 | Style reference: v2, or pull into the MVP? | Scope |
| Q12 | Body proportions: self-reported notes, or photo estimates? | Recommender |
| Q13 | Which Reminders lists? Weather location? °C or °F? | M1 |
| Q14 | Morning precompute time (06:00 assumed) | M3 |
| Q15 | Distance from camera to where the user can stand back, and mount height? | Framing |

---

## 6. Scope

**In the MVP:** weather, three calendars, reminders, presence-based display, QR pairing, bulk onboarding, review inbox, closet browser with edit, merge, retire and dirty/clean, outfit generator with collage, morning precompute, thumbs feedback, "worn today", style quiz and profile, capture flow, check-ins (U22), calibration and move detection, spend cap, backup.

**v2:** style boards, virtual try-on, voice, wear logging without confirmation, packing lists, gap analysis, remote access.

---

## 7. Architecture

```
 iPhone                        Raspberry Pi 4 (home Wi-Fi, fixed IP)                  Internet (outbound only)
┌───────────────┐   HTTP/WS   ┌──────────────────────────────────────────────┐
│ Web app       │◀───────────▶│ mirror-api (FastAPI)                         │──▶ fal: segmentation, bg removal
└───────────────┘             │  auth, routes, capture state machine, web app│──▶ Claude: tagging, outfits
┌───────────────┐   HTTP POST │                                              │──▶ Open-Meteo
│ Shortcut      │────────────▶│ worker: pipeline queue, scheduled jobs       │──▶ 3 iCal feeds
└───────────────┘             │ camera-daemon: camera, hall, radar, LEDs     │──▶ Cloudflare R2 backup
                              │ SQLite + sqlite-vec + images on USB SSD      │
                              │ MagicMirror² + MMM-Stylist (WS client)       │
                              └──────────────────────────────────────────────┘
```

Rules:

- The Pi never runs SAM-class or diffusion models.
- Exactly one process (`camera-daemon`) owns the camera and GPIO. `mirror-api` talks to it over a Unix socket.
- The capture state machine lives in `mirror-api`.
- The core mirror (clock, weather, calendar) must keep working if `mirror-api` or any API is down.
- Storage, AI providers and hardware each sit behind an interface with a swappable implementation.

### Proposed repository layout

```
MY-ror/
├── HANDOFF.md
├── mirror-model.html, deploy/      # existing 3D model
├── server/                         # Python
│   ├── mirror_api/                 # FastAPI app, routers, auth, websocket, config
│   ├── capture/                    # state machine, quality gate, calibration
│   ├── pipeline/                   # onboarding and check-in jobs, queue
│   ├── providers/                  # segmenter/ bg_remover/ embedder/ llm/
│   ├── recommender/                # rules, composer, validator, style profile
│   ├── integrations/               # calendars, weather, reminders
│   ├── hardware/                   # interfaces, pi drivers, mocks, camera_daemon
│   ├── db/                         # schema, migrations
│   └── tests/
├── mirror/MMM-Stylist/             # MagicMirror module
├── phone/                          # React + Vite
├── ops/                            # systemd units, install, backup scripts
└── eval/                           # golden set and bake-off scripts
```

Git-ignore `.env`, the database, all images and `eval/photos/`. They contain body photos and secrets.

---

## 8. Hardware

### Parts

| Part | Choice | Notes |
| --- | --- | --- |
| Computer | Raspberry Pi 4, 4 GB | Official 5.1 V / 3 A supply, heatsink, vented frame |
| Storage | USB 3 SSD (128–256 GB) | Boot and data. microSD only for setup. See C6 |
| Display cable | micro-HDMI to HDMI, right-angle | The Pi 4 has micro-HDMI |
| Display | 24" monitor, portrait, about 30 × 53 cm active | VESA, IPS, 300+ nits, must blank fully |
| Glass | Two-way glass, larger than the monitor | Real glass. Camera sits behind a non-display area |
| Camera | Pi Camera Module 3 **Wide** | 120° diagonal, 102° × 67° [VERIFIED]. In portrait, 102° is vertical |
| Baffle | Black foam ring around the lens | Pressed to the glass to stop screen flare |
| Lens cover | Manual slider, thumbwheel or magnetic slider | Lever about an inch at most (U13) |
| Cover sensor | TI DRV5032 FB or FC, TO-92 | See C3 and C4 |
| Magnet | N52 disc, 6 × 3 mm, nickel-plated | About 12 mT at 1 cm by calculation [UNTESTED] |
| Presence | HLK-LD2410C | 5 V supply, 3.3 V signals, range tuned to about 0–1.5 m |
| LEDs | COB strip, CRI 90+, about 5000K, 24 V preferred | About 2.7 m for a 50 × 85 cm glass |
| LED supply | 24 V, strip watts + 20% | Outside the frame (C1) |
| LED switch | AO3400A-class MOSFET or 5 V gate driver | See C2. Gate pulldown resistor, about 10 kΩ |
| Demister | Low-voltage film, cutout at the lens | Switched by a second MOSFET |
| Frame | About 6–8 cm deep, vented, removable back panel | Material open (Q6) |
| Mount | Cleat or Z-clip rated 25 kg+, into studs | Total weight about 10–12 kg [UNTESTED] |

Rough remaining cost: $250–350, excluding Pi, monitor and glass. Prices were not checked.

### Proposed wiring [UNTESTED]

| Signal | BCM | Physical pin | Notes |
| --- | --- | --- | --- |
| Hall sensor OUT | GPIO17 | 11 | Internal pull-up, active low = closed. 50 ms debounce. A broken wire reads "open" |
| Hall VCC | 3.3 V | 1 | |
| LD2410C OUT | GPIO27 | 13 | Presence |
| LD2410C UART | GPIO14 / 15 | 8 / 10 | Optional tuning. Disable the serial console |
| LD2410C VCC | 5 V | 2 | |
| LED gate | GPIO18 | 12 | Hardware PWM. 100% duty at capture |
| Demister gate | GPIO23 | 16 | |
| Ground | GND | 6, 9, 14 | Common ground with the LED supply |

Mount the radar in the frame edge behind plastic or wood. The mirror coating may block it.

### First hardware test

Run before designing the frame. It must go fully black, with no "No signal" box:

```bash
wlr-randr --output HDMI-A-1 --off
```

Turn it back on with:

```bash
wlr-randr --output HDMI-A-1 --on
```

Output names vary. Run `wlr-randr` alone to list them.

---

## 9. Capture flow

States: `idle → cover_wait → preview → countdown → flash → quality_check → review → cover_reminder → idle`

| State | Behavior | Exits |
| --- | --- | --- |
| `idle` | Camera closed | Phone taps Take photo |
| `cover_wait` | Mirror: "Slide the camera cover open" | Cover opens, or 60 s timeout, or cancel |
| `preview` | Live preview with body outline | After about 1.5 s |
| `countdown` | 3-2-1 with beeps | Abort if the cover closes |
| `flash` | Screen ramps to white over about 200 ms, LEDs 100% duty, capture with locked white balance, exposure and gain | Frame captured |
| `quality_check` | Reject black/occluded, blurred, fogged, no person | Pass, or a specific message and retry |
| `review` | Shot shown 2 s; phone Keep or Retake | User choice, 60 s timeout |
| `cover_reminder` | Amber banner until the sensor reads closed | Cover closed |

Edge cases: sensor says open but frame is black (cover partly closed or sensor fault); app restart mid-capture returns to `idle`; only one capture at a time.

**Framing:** the counter hides the lower body at the sink, so assume chest or waist up [UNTESTED]. Full-body and add-item photos use the phone.

**Calibration:** lock camera gains under capture light, and store a background reference patch.

| Observation (person masked out) | Meaning | Action |
| --- | --- | --- |
| Same structure, color shifted | Lighting change | Auto-correct |
| Structure changed | Mirror moved | "New room detected. Recalibrate?" |
| All matched items drift the same way | Global change | Flag for recalibration |

Locked exposure with changing window light may over- or under-expose. Test with the bathroom's real lighting.

---

## 10. AI pipeline

### Onboarding (one garment per phone photo, on a white sheet)

1. Upload one file per request, resumable.
2. Decode HEIC (`pillow-heif`), strip EXIF, fix orientation.
3. White-balance against the sheet.
4. Remove background.
5. Tag with a vision LLM against a fixed taxonomy.
6. Embed (FashionCLIP).
7. Duplicate check (cosine threshold, LLM tie-break).
8. Place in the review inbox.

No segmentation model is needed here.

### Check-in (mirror photo)

1. Face blur on the Pi.
2. Vision LLM lists garments with labels and boxes.
3. Segmenter turns boxes into masks.
4. Crop, tag, embed, match to closet.
5. "Did you wear these?" One tap writes the wear log.

### Taxonomy (to be written in M2)

`category`, `subcategory`, `primary_color`, `secondary_color` (named bucket plus Lab), `pattern`, `material_guess`, `formality` 1–5, `season`, `fit`, `style_tags`. Every field is a fixed list except Lab and the numeric score. Reject unknown values and retry once.

### fal SAM 3 notes [VERIFIED schema, UNTESTED behavior]

- Endpoint `fal-ai/sam-3/image`, $0.005 per request.
- `prompt` **defaults to `"wheel"`**. Always set it explicitly, and test how it interacts with `box_prompts`.
- `box_prompts` is a list of `{x_min, y_min, x_max, y_max, object_id}` with integer values. Omit `object_id` for separate objects.
- `return_multiple_masks` defaults to false and `max_masks` to 3 (range 1–32). Set both, or masks will be lost.
- `apply_mask` defaults to true. Set it to false to get masks.
- `include_scores` and `include_boxes` default to false. No confidence threshold parameter is listed.

### Claude notes

Load the `claude-api` skill before writing Claude code. Model IDs: `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. Use structured outputs (`output_config.format`), because forced `tool_choice` returns a 400 on Sonnet 5.5 and Opus 5.5. Don't send sampling parameters. Set `effort` explicitly, since thinking adds output cost. Check `stop_reason` for `refusal`. Test whether Claude's boxes are tight enough for segmentation.

### Recommender

Stage 1, deterministic filter [PROPOSED starting values]:

| Signal | Rule |
| --- | --- |
| Below 10 °C | Outer layer required |
| Above 25 °C | Short sleeves and light fabrics preferred |
| Rain | No suede or canvas shoes; suggest a rain layer |
| Calendar | Highest event formality sets the minimum |
| Laundry | Exclude dirty items |
| Recently worn | Down-rank for N days per category |
| Season | Exclude off-season unless weather overrides |

Stage 2: the LLM returns 3 outfits as item IDs with a one-line reason. The server rejects any ID outside the candidate list, retries once, then falls back to rules only.

Precompute at 06:00. Rebuild if weather or calendar changed when presence is next detected.

Style learning: quiz → editable plain-language profile → weekly update from feedback and wear logs. No fine-tuning.

---

## 11. Interfaces

### Screens

- **Mirror:** Off, Ambient, Outfit, Capture, Status. Put images on a light card, because dark pixels read as mirror.
- **Phone:** Pair, Today, Capture, Add items, Review inbox, Closet, Style, Settings.

### API sketch [PROPOSED]

| Route | Purpose |
| --- | --- |
| `POST /api/pair` | Exchange a QR code for a device token |
| `GET /api/today` | Weather, events, reminders, outfit |
| `POST /api/outfits/reroll`, `POST /api/outfits/{id}/feedback` | Outfits |
| `POST /api/items/upload`, `GET/PATCH /api/items`, `POST /api/items/{id}/worn` | Closet |
| `GET /api/inbox`, `POST /api/inbox/{id}/{confirm,merge,reject}` | Review |
| `POST /api/capture`, `POST /api/capture/{id}/{keep,retake,cancel}` | Capture |
| `POST /api/reminders` | Shortcut push |
| `GET /api/calendar.ics` | Merged feed for MagicMirror (local only) |
| `GET/PUT /api/style`, `GET/PUT /api/settings`, `GET /api/health` | Profile, settings, status |
| `DELETE /api/photos/{id}` | Delete a photo and everything derived from it |
| `WS /ws` | Push to mirror and phone |

Every route needs a device token, with two exceptions: `POST /api/pair`, and `calendar.ics`, which answers localhost requests only.

---

## 12. Data model

SQLite with `sqlite-vec`. Image files on the SSD; paths in the database.

| Table | Key fields |
| --- | --- |
| `photos` | id, source, path, taken_at, calibration_id, quality_flags |
| `detections` | id, photo_id, box, mask_path, raw_tags, embedding, status |
| `items` | id, name, taxonomy fields, cutout_path, embedding, state (clean, dirty, retired) |
| `outfits` | id, created_at, context, reasoning, source |
| `outfit_items` | outfit_id, item_id, slot |
| `feedback` | outfit_id, value, at |
| `wear_log` | item_id, date, source |
| `style_profile` | version, text, quiz, updated_at |
| `reminders` | id, title, due, list, synced_at |
| `calibrations` | id, created_at, gains, background_ref |
| `devices` | id, name, token_hash, paired_at |
| `jobs` | id, type, payload, status, attempts, error |
| `api_spend` | id, at, provider, model, units, est_usd |

Log feedback, wears and skipped suggestions from day one.

---

## 13. Integrations

| Source | Method | Notes |
| --- | --- | --- |
| Weather | Open-Meteo, no key | |
| Google | Secret address in iCal format | May lag |
| Outlook | Published calendar ICS link | May lag |
| iCloud | Public Calendar `webcal://` link | Anyone with the link can read it. Keep in `.env` |
| Reminders | iPhone Shortcut: Find Reminders → JSON → `POST /api/reminders` with a token | Read-only. No Reminders web API exists |

Shortcut triggers: joining home Wi-Fi; 06:30, 12:00 and 18:00; closing the Reminders app. Show the last sync time.

Parse with `icalendar` and `recurring-ical-events`. Merge events with the same title and start time.

---

## 14. Privacy, security, guardrails

### What leaves the house

| Data | Goes to | When |
| --- | --- | --- |
| Full phone photo of a garment | Background-removal host | Onboarding, if hosted |
| Garment cutout | Claude | Tagging |
| Face-blurred body photo | Claude and fal | Check-ins (U24) |
| Event titles | Claude | Formality tagging |
| Closet tags, style profile, weather | Claude | Outfits |
| Encrypted database and images | R2 | Nightly |

Check fal's data retention terms before sending the first real photo (not yet done). If the face blur fails or finds no face, stop and send nothing. Local background removal on the Pi would remove the first row; measure its speed first.

### Controls

- Manual cover is the primary control. Capture only on an explicit tap. No streaming, no background capture.
- "Camera uncovered" reminder whenever the cover is open.
- Device tokens from QR pairing. Store hashes only.
- **Plain HTTP (U23)** sends tokens and photos unencrypted over Wi-Fi, so anyone on that network could capture them. Use a strong WPA2/WPA3 password and keep guests and smart devices on a guest network.
- Plain HTTP rules out service workers, in-page camera and push. Use the file input for photos, and pair by scanning the QR code with the iPhone Camera app.
- HTTPS can be added later without a redesign: a domain with a DNS-challenge certificate pointing at the Pi's local IP.
- Secrets in `.env`, outside git. Encrypted backups; store the key somewhere other than the Pi.
- Schema-validate every LLM response. Whitelist item IDs. Low-confidence results go to the inbox.
- System prompt forbids body or weight judgments and inferring sensitive traits.
- One-tap deletion of a photo and its derivatives. Note that backups keep a copy until they expire.

---

## 15. Budget

Claude prices per million tokens (input / output): Haiku 5.5 $0.10 / $0.50, Sonnet 5.5 $2 / $10, Opus 5.5 $4 / $20 (from the `claude-api` skill, cached 2026-11-03).

| Item | Assumption | Sonnet, 1 call | Opus, SAM 3.1 × 5 |
| --- | --- | --- | --- |
| Outfits | 60 calls, 5K in / 1.5K out | $1.50 | $3.00 |
| Style update | 4 runs | $0.12 | $0.24 |
| Segmentation | 30 check-ins | $0.15 | $1.50 |
| Tagging, formality | Haiku | $0.03 | $0.03 |
| **Monthly total** | | **about $1.80** | **about $4.77** |

With Sonnet and one SAM 3 call per garment (5 per check-in), the total is about $2.40. Background removal is extra: fal lists BiRefNet at "$0 per compute seconds", so confirm the real price. Thinking tokens may raise outfit cost.

Volumes assume daily check-ins (360 a year), slightly above U8.

Controls: log every paid call before returning. At 80% of the cap, rerolls use cached outfits. At 100%, outfits use rules only and check-ins queue.

Electricity: monitor plus Pi at about 25 W, 24/7, is about 18 kWh a month [UNTESTED].

---

## 16. Development and testing

| Stage | Development (3080) | Production | Switch |
| --- | --- | --- | --- |
| Segmentation | SAM 3 if it fits (C16), else SegFormer or Grounded SAM 2 | fal | `SEGMENTER` |
| Background removal | BiRefNet | Hosted, or Pi if fast enough | `BG_REMOVER` |
| Embeddings | FashionCLIP | Pi CPU [UNTESTED], else hosted | `EMBEDDER` |
| LLM | Claude (P10) | Claude | `LLM` |

- Embeddings must come from the same model in both, or the closet must be re-embedded.
- Hardware mocks let the whole backend run on a laptop.
- **Golden set:** 30–50 of the user's own photos with hand-checked labels.
- **Bake-off:** SegFormer clothes parser, Grounded SAM 2, SAM 3 text prompts, SAM 3 boxes, SAM 3.1. Score masks by eye.
- Re-run the golden set whenever a prompt or model changes.
- Unit-test the capture state machine, rule filter, ID validator, spend cap and calendar merge.

---

## 17. Operations

- MagicMirror 2.38.0 requires Node `>=22.22.2 <23 || >=24` [VERIFIED 2026-11-25].
- systemd with restart-always: `magicmirror`, `mirror-api`, `camera-daemon`, `worker`. Hardware watchdog on.
- DHCP reservation for a fixed IP. SSH on the home network only.
- Nightly `rclone crypt` to R2, using a consistent SQLite snapshot. Test a restore monthly.
- Deploy with `git pull` and a restart. No auto-updates.
- Measure memory early: MagicMirror, the API and the worker share 4 GB [UNTESTED].

---

## 18. Milestones

### M0: hardware proof and stock mirror

- [ ] Answer Q3 and Q4; measure the glass and monitor
- [ ] Pi boots from SSD; SSH and fixed IP work
- [ ] Monitor sleep test passes
- [ ] MagicMirror in portrait with weather
- [ ] LD2410C turns the display on and off
- [ ] Check brightness through the actual glass
- **Gate:** monitor blanks fully, and the display follows presence

### M1: backend, pairing, integrations

- [ ] `git init`, repo layout, config, `.gitignore`
- [ ] FastAPI skeleton, database, migrations, hardware mocks
- [ ] QR pairing and tokens over plain HTTP (U23)
- [ ] Merged calendars and reminders Shortcut
- [ ] MMM-Stylist over WebSocket
- [ ] systemd units and nightly backup with a restore test
- **Gate:** reminders and all three calendars on the mirror; core survives the API being stopped

### M2: closet onboarding

- [ ] Taxonomy and golden set
- [ ] Upload, queue, background removal, tagging, embedding, duplicates
- [ ] Review inbox, closet browser, editing
- [ ] Style quiz; spend ledger and cap
- **Gate:** 30+ real items in one afternoon

### M3: outfit generator

- [ ] Rules, composer, validator, fallback
- [ ] Precompute, collage, feedback, "worn today"
- [ ] Style profile updates
- **Gate:** a sensible outfit within 2 s of walking up; zero invalid IDs

### M4: capture and check-ins

- [ ] Camera bracket, baffle, cover, hall sensor on the bench
- [ ] Capture state machine, flash, quality gate
- [ ] Calibration and move detection
- [ ] Bake-off, then check-in matching (U22, U24)
- [ ] Check fal's retention terms; test that a failed face blur blocks the upload
- [ ] Framing and fog tests in the real bathroom
- **Gate:** capture works end to end; matches confirmed in one tap; a move triggers recalibration

### M5: final assembly [PROPOSED, new]

- [ ] Electrical plan reviewed (C1)
- [ ] Bench burn-in, about a week, including heat and humidity
- [ ] Assemble, mount, recalibrate, tune radar in place
- [ ] Confirm every serviceable part comes out through the back
- **Gate:** one week mounted with no manual intervention

---

## 19. Risks

| Risk | Mitigation |
| --- | --- |
| Monitor shows "No signal" | Sleep test first |
| Mains power in a bathroom | Low voltage in the frame, electrician review |
| Steam and humidity | Demister, quality gate, venting, burn-in |
| Too little light through the glass | Flash, LEDs; test the camera through the real glass before M4 |
| Counter blocks the body | Phone for full-body photos; one-tap "which pants?" |
| SAM 3 won't fit or access is refused | SegFormer, Grounded SAM 2, or fal in development |
| Prompts don't transfer between models | Same Claude models in development and production (P10) |
| Pi too slow or short on memory | Measure in M1–M2; offload to hosted |
| Reminders are stale | Several triggers; show last sync |
| Empty closet | Onboarding and quiz before outfits |

---

## 20. Start here

1. Read sections 3, 4 and 5.
2. Get Q3 and Q4 (measurements) before any hardware design. The rest can wait.
3. Tell the user about C1–C6 if they have not bought parts yet.
4. Hardware track: run the monitor sleep test.
5. Software track: `git init`, then M1 with mocks. It needs no hardware.

## 21. Working notes

- The user prefers debating trade-offs over being handed an answer, and makes quick decisions.
- They value durability, low cost and a clean look on the assembled mirror.
- Say plainly what was not tested.
- Don't scrape Instagram or Pinterest (U19).

## 22. Sources checked

- MagicMirror [`package.json`](https://raw.githubusercontent.com/MagicMirrorOrg/MagicMirror/master/package.json) (2026-11-25)
- fal [SAM 3 docs](https://fal.ai/models/fal-ai/sam-3/image/llms.txt) and [OpenAPI schema](https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=fal-ai/sam-3/image), [SAM 3.1](https://fal.ai/models/fal-ai/sam-3-1/image/llms.txt), [BiRefNet](https://fal.ai/models/fal-ai/birefnet/llms.txt) (2026-11-25)
- Search summaries only, pages not opened: TI [DRV5032 datasheet](https://www.ti.com/lit/gpn/drv5032); [AOD4184A](https://www.aosmd.com/res/datasheets/AOD4184A.pdf), [AO3400A](https://www.aosmd.com/res/datasheets/AO3400A.pdf) and [IRLZ44](https://www.vishay.com/en/product/91328/); [Camera Module 3](https://raspberrypi.com/products/camera-module-3); [SAM 3 repo](https://github.com/facebookresearch/sam3) and [install guide](https://www.mintlify.com/facebookresearch/sam3/installation)

Items marked [VERIFIED] from the last line rest on search summaries. Confirm them in the datasheet before ordering.
