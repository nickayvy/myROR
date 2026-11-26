/* MMM-Stylist node_helper: the only part that talks to mirror-api.
 *
 * Runs on the Pi, so every request is from 127.0.0.1 and needs no token (HANDOFF A8).
 * Uses Node's built-in fetch and WebSocket (Node >= 22). Never throws into MagicMirror:
 * when the API is down it reports OFFLINE and retries with backoff.
 *
 * Database etiquette (HANDOFF A3): nothing here polls the API on a timer while idle.
 * GET /api/today is served from memory; outfits (which read the database and carry
 * 15-minute image links) are fetched on connect, on `today.updated`, when someone walks
 * up after a while, and when an image link has expired.
 */
const NodeHelper = require("node_helper");
const Log = require("logger");

const OUTFIT_REFRESH_AFTER_MS = 10 * 60 * 1000; // image links last 15 min
const SHOWN_REPEAT_MS = 30 * 60 * 1000; // report the same outfit as shown at most this often

module.exports = NodeHelper.create({
	start () {
		this.cfg = null;
		this.ws = null;
		this.attempt = 0;
		this.retryTimer = null;
		this.pingTimer = null;
		this.online = false;
		this.outfitsAt = 0;
		this.lastShown = new Map();
		this.lastImageRefresh = 0;
		this.state = { today: null, outfits: null, status: null, capture: { state: "idle" }, cover: null, present: null };
	},

	stop () {
		clearTimeout(this.retryTimer);
		clearInterval(this.pingTimer);
		try { this.ws && this.ws.close(); } catch (e) { /* ignore */ }
	},

	base () {
		return `http://${this.cfg.apiHost}:${this.cfg.apiPort}`;
	},

	async api (path, opts = {}) {
		const ctrl = new AbortController();
		const t = setTimeout(() => ctrl.abort(), opts.timeout || 8000);
		try {
			const res = await fetch(this.base() + path, {
				method: opts.method || "GET",
				headers: opts.body !== undefined ? { "Content-Type": "application/json" } : {},
				body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
				signal: ctrl.signal
			});
			if (res.status === 204) return null;
			const text = await res.text();
			const data = text ? JSON.parse(text) : null;
			if (!res.ok) {
				const err = new Error(`${path} -> ${res.status}`);
				err.status = res.status;
				throw err;
			}
			return data;
		} finally {
			clearTimeout(t);
		}
	},

	/** Links the API returns relative to itself (e.g. /api/capture/{id}/image, local-storage
	 * /api/files/...) must point at the API, not at MagicMirror's own server. */
	abs (url) {
		return typeof url === "string" && url.startsWith("/") ? (this.cfg.browserApiBase || this.base()) + url : url;
	},

	fixOutfits (payload) {
		const outfits = ((payload && payload.outfits) || []).map((o) => ({
			...o,
			collage_url: this.abs(o.collage_url),
			items: (o.items || []).map((i) => ({ ...i, cutout_url: this.abs(i.cutout_url) }))
		}));
		return { ...payload, outfits };
	},

	send (type, payload) {
		this.sendSocketNotification(type, payload);
	},

	setOnline (online) {
		if (online !== this.online) {
			this.online = online;
			this.send("ONLINE", { online, at: Date.now() });
		}
	},

	socketNotificationReceived (n, payload) {
		if (n === "INIT") {
			const first = !this.cfg;
			this.cfg = payload;
			if (first) this.connect();
			else this.pushAll(); // a browser reloaded: give it the current picture
		} else if (n === "SHOWN") {
			void this.reportShown(payload.id);
		} else if (n === "PAIR_REQUEST") {
			void this.pairCode();
		} else if (n === "REFRESH_OUTFITS") {
			if (Date.now() - this.lastImageRefresh > 60 * 1000) {
				this.lastImageRefresh = Date.now();
				void this.loadOutfits();
			}
		}
	},

	pushAll () {
		this.send("ONLINE", { online: this.online, at: Date.now() });
		const s = this.state;
		if (s.today) this.send("TODAY", s.today);
		if (s.outfits) this.send("OUTFITS", s.outfits);
		if (s.status) this.send("STATUS", s.status);
		this.send("CAPTURE", s.capture);
		if (s.cover !== null) this.send("COVER", { open: s.cover });
		if (s.present !== null) this.send("PRESENCE", { present: s.present });
	},

	connect () {
		clearTimeout(this.retryTimer);
		const url = `ws://${this.cfg.apiHost}:${this.cfg.apiPort}/ws`;
		let ws;
		try {
			ws = new WebSocket(url);
		} catch (e) {
			this.schedule();
			return;
		}
		this.ws = ws;
		ws.onmessage = (ev) => {
			let msg;
			try { msg = JSON.parse(ev.data); } catch (e) { return; }
			this.onEvent(msg);
		};
		ws.onclose = () => {
			clearInterval(this.pingTimer);
			if (this.ws === ws) this.ws = null;
			this.setOnline(false);
			this.schedule();
		};
		ws.onerror = () => { /* onclose follows */ };
	},

	schedule () {
		clearTimeout(this.retryTimer);
		const delay = Math.min(60000, 1000 * 2 ** this.attempt) * (0.75 + Math.random() * 0.5);
		this.attempt = Math.min(this.attempt + 1, 6);
		this.retryTimer = setTimeout(() => this.connect(), delay);
	},

	async refreshAll () {
		await Promise.allSettled([this.loadToday(), this.loadOutfits(), this.loadStatus(), this.loadCapture(), this.checkPairing()]);
	},

	async loadToday () {
		try {
			this.state.today = await this.api("/api/today");
			this.send("TODAY", this.state.today);
		} catch (e) {
			Log.warn(`[MMM-Stylist] today: ${e.message}`);
		}
	},

	async loadOutfits () {
		try {
			this.state.outfits = this.fixOutfits(await this.api("/api/outfits"));
			this.outfitsAt = Date.now();
			this.send("OUTFITS", this.state.outfits);
		} catch (e) {
			Log.warn(`[MMM-Stylist] outfits: ${e.message}`);
		}
	},

	async loadStatus () {
		try {
			this.state.status = await this.api("/api/today/status");
			this.send("STATUS", this.state.status);
		} catch (e) {
			Log.warn(`[MMM-Stylist] status: ${e.message}`);
		}
	},

	async loadCapture () {
		try {
			const c = await this.api("/api/capture/current");
			if (c) this.onEvent({ type: "capture.state", data: c });
		} catch (e) {
			// 404 until the capture routes land; nothing to show.
		}
	},

	/** Show the pairing QR by itself when no phone has been paired yet. */
	async checkPairing () {
		if (this.cfg.pairWhenUnpaired === false) return;
		try {
			const devices = await this.api("/api/devices");
			if (!devices.some((d) => d.kind === "phone" && !d.revoked_at)) await this.pairCode();
		} catch (e) {
			Log.warn(`[MMM-Stylist] devices: ${e.message}`);
		}
	},

	async pairCode () {
		try {
			const body = this.cfg.phoneBaseUrl ? { base_url: this.cfg.phoneBaseUrl } : {};
			const r = await this.api("/api/pair/code", { method: "POST", body });
			this.send("PAIR", { code: r.code, qr: r.qr_png, url: r.pair_url, expiresAt: Date.now() + r.expires_in * 1000 });
		} catch (e) {
			Log.warn(`[MMM-Stylist] pair code: ${e.message}`);
		}
	},

	async reportShown (id) {
		const last = this.lastShown.get(id) || 0;
		if (Date.now() - last < SHOWN_REPEAT_MS) return;
		this.lastShown.set(id, Date.now());
		try {
			await this.api(`/api/outfits/${id}/shown`, { method: "POST" });
		} catch (e) {
			this.lastShown.delete(id);
			Log.warn(`[MMM-Stylist] shown: ${e.message}`);
		}
	},

	onEvent (msg) {
		const d = msg.data || {};
		switch (msg.type) {
			case "hello":
				this.attempt = 0;
				this.setOnline(true);
				clearInterval(this.pingTimer);
				this.pingTimer = setInterval(() => {
					try { this.ws && this.ws.send(JSON.stringify({ type: "ping" })); } catch (e) { /* ignore */ }
				}, 30000);
				void this.refreshAll();
				break;
			case "today.updated":
				this.state.today = d;
				this.send("TODAY", d);
				// The pushed payload carries outfits with fresh collage links: use them
				// directly instead of reading the database again.
				if (Array.isArray(d.outfits) && d.outfits.length && d.outfits.every((o) => o.collage_url)) {
					this.state.outfits = this.fixOutfits({ outfits: d.outfits });
					this.outfitsAt = Date.now();
					this.send("OUTFITS", this.state.outfits);
				} else {
					void this.loadOutfits();
				}
				void this.loadStatus();
				break;
			case "outfits.updated":
			case "outfits.worn":
				void this.loadOutfits();
				break;
			case "capture.state":
				this.state.capture = { ...d, review_url: this.abs(d.review_url) };
				this.send("CAPTURE", this.state.capture);
				break;
			case "cover":
				this.state.cover = !!d.open;
				this.send("COVER", { open: !!d.open });
				break;
			case "presence":
				this.state.present = !!d.present;
				this.send("PRESENCE", { present: !!d.present });
				if (d.present && Date.now() - this.outfitsAt > OUTFIT_REFRESH_AFTER_MS) void this.loadOutfits();
				break;
			case "pair.show":
				void this.pairCode();
				break;
			case "pair.done":
				this.send("PAIR_DONE", d);
				break;
			default:
				break;
		}
	}
});
