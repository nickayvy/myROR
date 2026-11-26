# MMM-Stylist

MagicMirror² module for the bathroom mirror: today's outfit (collage on a light card with its
one-line reason, alternatives cycled slowly), open reminders, the camera capture flow, the
pairing QR, and small status notes. It talks only to mirror-api on the same Pi
(`http://127.0.0.1:<port>`, `ws://127.0.0.1:<port>/ws`); localhost needs no token.

Tested with MagicMirror 2.38.0 (Node `>=22.22.2 <23 || >=24`). No npm dependencies: the
node_helper uses Node's built-in `fetch` and `WebSocket`.

## Install on the Pi

```bash
cd ~/MagicMirror/modules
ln -s ~/MY-ror/mirror/MMM-Stylist MMM-Stylist   # or copy the folder
```

Then add the module to `~/MagicMirror/config/config.js`. `config.sample.js` in this folder is a
complete example for a portrait 1080x1920 screen: clock, the stock weather module, the stock
calendar module pointed at `http://127.0.0.1:<port>/api/calendar.ics` (mirror-api's merged
feed, localhost only), and this module:

```js
{
	module: "MMM-Stylist",
	position: "lower_third",
	config: {
		apiPort: 8000,        // mirror-api's port
		phoneBaseUrl: null,   // what the pairing QR opens, e.g. "http://192.168.1.20:8000"
		offDelay: 180000      // blank everything 3 min after the presence sensor goes quiet
	}
}
```

Restart MagicMirror (`pm2 restart mm` or the systemd unit).

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `apiHost`, `apiPort` | `127.0.0.1`, `8000` | where node_helper reaches mirror-api |
| `browserApiBase` | `http://apiHost:apiPort` | where the *browser* loads API-relative images (capture review). Only change it when viewing MagicMirror from another machine |
| `offDelay` | 180000 | ms without presence before blanking; `0` never blanks |
| `blankOtherModules` | `true` | "off" hides every module, not only this one |
| `cycleInterval` | 25000 | ms per outfit before showing the next alternative |
| `maxOutfits`, `maxReminders` | 3, 5 | |
| `showReason`, `showReminders` | `true` | |
| `phoneBaseUrl` | `null` | base URL in the pairing QR (default: the Pi's LAN address as mirror-api sees it) |
| `pairWhenUnpaired` | `true` | show the QR by itself until a phone is paired, refreshing the code as it expires |
| `staleAfterHours` | 3 | reminders sync older than this is shown in amber |

Notifications it accepts from other modules: `STYLIST_SHOW_PAIRING`, `STYLIST_HIDE_PAIRING`.

## States

- **Off**: the `presence` event says nobody is there for `offDelay`: all modules hide (black
  glass). Never while a capture or pairing is on screen. Presence brings them back.
- **Ambient**: no outfit available: only reminders and the stock modules.
- **Outfit**: the collage card plus reason; each outfit actually displayed is reported with
  `POST /api/outfits/{id}/shown` (at most once per outfit every 30 min).
- **Capture**: full-screen overlay driven by `capture.state`: cover prompt, preview outline,
  3-2-1, full-white flash, quality messages, review image. An amber "camera uncovered" banner
  stays at the top whenever the `cover` sensor reads open outside an active shot.
- **Status**: one dim line: backend offline (last data stays on screen), out-of-date sources
  (`stale` on the today payload, weather/calendar status), last reminders sync, AI budget
  reached. Pairing QR with the code in text and an expiry countdown.

## Failing quietly

If mirror-api is down the node_helper reconnects with backoff (up to 60 s) and the module
keeps its last outfit and reminders with an "offline" note. MagicMirror's own clock, weather
and calendar keep working. Nothing here polls the database while idle: `/api/today` is served
from memory; outfits are re-read on connect, on `today.updated` (using its own collage links
when present), when someone walks up more than 10 min after the last read, and when an image
link has expired.
