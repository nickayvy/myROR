/* global Module, Log, MM */
/* MMM-Stylist: today's outfit, reminders, the camera capture flow and pairing on the mirror.
 *
 * Two-way glass: black pixels read as mirror, so everything is light on pure black; the
 * only large bright areas are the outfit card (images need a light background), the
 * pairing QR card, and the deliberate full-white capture flash.
 *
 * States: off (nobody present for `offDelay`), ambient (no outfit to show), outfit,
 * capture (full-screen overlay), plus status bits (offline, stale data, pairing QR).
 * If the backend is down the module shows its last data with a small "offline" note
 * and MagicMirror's own clock/weather/calendar modules carry on unaffected.
 */
Module.register("MMM-Stylist", {
	defaults: {
		apiHost: "127.0.0.1",
		apiPort: 8000,
		offDelay: 3 * 60 * 1000, // blank this long after the presence sensor goes quiet (0 = never)
		blankOtherModules: true, // "off" hides every module, not just this one
		cycleInterval: 25 * 1000, // time on each outfit before showing the next alternative
		maxOutfits: 3,
		showReminders: true,
		maxReminders: 5,
		showReason: true,
		phoneBaseUrl: null, // e.g. "http://192.168.1.20:8000" (what the pairing QR opens)
		browserApiBase: null, // where the *browser* reaches the API for images; default http://apiHost:apiPort
		pairWhenUnpaired: true, // show the pairing QR by itself until a phone is paired
		staleAfterHours: 3 // flag reminders as stale when the last push is older than this
	},

	getStyles () {
		return ["MMM-Stylist.css"];
	},

	start () {
		this.today = null;
		this.outfits = [];
		this.status = null;
		this.online = null;
		this.capture = { state: "idle" };
		this.coverOpen = false;
		this.present = true; // assume someone is there until the sensor says otherwise
		this.idx = 0;
		this.pair = null;
		this.offTimer = null;
		this.cycleTimer = null;
		this.isOff = false;
		this.root = null;
		this.overlay = null;
		this.banner = null;
		this.sendSocketNotification("INIT", {
			apiHost: this.config.apiHost,
			apiPort: this.config.apiPort,
			phoneBaseUrl: this.config.phoneBaseUrl,
			browserApiBase: this.config.browserApiBase,
			pairWhenUnpaired: this.config.pairWhenUnpaired
		});
		setInterval(() => this.renderStatus(), 60 * 1000); // keeps "x min ago" honest; no network
	},

	notificationReceived (n) {
		if (n === "DOM_OBJECTS_CREATED") {
			this.ensureBodyLayers();
			this.render();
		} else if (n === "STYLIST_SHOW_PAIRING") {
			this.sendSocketNotification("PAIR_REQUEST");
		} else if (n === "STYLIST_HIDE_PAIRING") {
			this.pair = null;
			this.renderPair();
		} else if (n === "USER_PRESENCE") {
			// Compatible with presence modules such as MMM-PIR-Sensor.
		}
	},

	socketNotificationReceived (n, p) {
		switch (n) {
			case "ONLINE":
				this.online = p.online;
				this.renderStatus();
				break;
			case "TODAY":
				this.today = p;
				this.renderReminders();
				this.renderStatus();
				break;
			case "OUTFITS":
				this.outfits = (p && p.outfits ? p.outfits : []).slice(0, this.config.maxOutfits);
				if (this.idx >= this.outfits.length) this.idx = 0;
				this.renderOutfit();
				break;
			case "STATUS":
				this.status = p;
				this.renderStatus();
				break;
			case "CAPTURE":
				this.capture = p || { state: "idle" };
				if (this.capture.state && this.capture.state !== "idle") this.setPresent(true);
				this.renderCapture();
				break;
			case "COVER":
				this.coverOpen = !!p.open;
				this.renderCapture();
				break;
			case "PRESENCE":
				this.setPresent(!!p.present);
				break;
			case "PAIR":
				this.pair = p;
				this.setPresent(true);
				this.renderPair();
				break;
			case "PAIR_DONE":
				this.pair = null;
				this.renderPair();
				break;
			default:
				break;
		}
	},

	/* ---------- presence / off ---------- */

	setPresent (present) {
		this.present = present;
		clearTimeout(this.offTimer);
		if (present) {
			if (this.isOff) this.wake();
			return;
		}
		if (!this.config.offDelay) return;
		this.offTimer = setTimeout(() => this.sleep(), this.config.offDelay);
	},

	sleep () {
		if (this.busy()) return; // never blank mid-capture or while pairing
		this.isOff = true;
		this.stopCycle();
		const mods = this.config.blankOtherModules ? MM.getModules() : [this];
		mods.enumerate((m) => m.hide(1500, () => {}, { lockString: "stylist-off" }));
	},

	wake () {
		this.isOff = false;
		const mods = this.config.blankOtherModules ? MM.getModules() : [this];
		mods.enumerate((m) => m.show(600, () => {}, { lockString: "stylist-off" }));
		this.renderOutfit();
	},

	busy () {
		return (this.capture && this.capture.state && this.capture.state !== "idle") || !!this.pair;
	},

	/* ---------- DOM ---------- */

	getDom () {
		if (!this.root) {
			const root = document.createElement("div");
			root.className = "stylist";
			root.innerHTML = `
				<div class="stylist-pair" hidden></div>
				<div class="stylist-outfit" hidden></div>
				<div class="stylist-reminders" hidden></div>
				<div class="stylist-status"></div>`;
			this.root = root;
		}
		return this.root;
	},

	part (sel) {
		return this.root ? this.root.querySelector(sel) : null;
	},

	ensureBodyLayers () {
		if (!this.overlay) {
			this.overlay = document.createElement("div");
			this.overlay.className = "stylist-capture";
			this.overlay.hidden = true;
			document.body.appendChild(this.overlay);
		}
		if (!this.banner) {
			this.banner = document.createElement("div");
			this.banner.className = "stylist-cover-banner";
			this.banner.hidden = true;
			this.banner.textContent = "Camera uncovered · slide the cover closed";
			document.body.appendChild(this.banner);
		}
	},

	render () {
		this.renderOutfit();
		this.renderReminders();
		this.renderStatus();
		this.renderCapture();
		this.renderPair();
	},

	esc (s) {
		return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]));
	},

	/* ---------- outfit ---------- */

	stopCycle () {
		clearInterval(this.cycleTimer);
		this.cycleTimer = null;
	},

	renderOutfit () {
		const el = this.part(".stylist-outfit");
		if (!el) return;
		const o = this.pair ? null : this.outfits[this.idx]; // the pairing QR takes the space
		if (!o) {
			el.hidden = true; // ambient: the stock modules and reminders only
			el.innerHTML = "";
			this.stopCycle();
			return;
		}
		const img = o.collage_url
			? `<img class="stylist-collage" alt="" src="${this.esc(o.collage_url)}">`
			: `<div class="stylist-grid n${Math.min(4, o.items.length)}">${o.items
				.slice(0, 4)
				.map((i) => (i.cutout_url ? `<img alt="" src="${this.esc(i.cutout_url)}">` : `<div class="stylist-noimg">${this.esc(i.name || i.slot)}</div>`))
				.join("")}</div>`;
		const dots = this.outfits.length > 1 ? `<div class="stylist-dots">${this.outfits.map((_, k) => `<span class="${k === this.idx ? "on" : ""}"></span>`).join("")}</div>` : "";
		el.innerHTML = `
			<div class="stylist-label">Today's outfit${this.outfits.length > 1 ? ` · ${this.idx + 1} of ${this.outfits.length}` : ""}</div>
			<div class="stylist-card">${img}</div>
			${this.config.showReason && o.reasoning ? `<div class="stylist-reason">${this.esc(o.reasoning)}</div>` : ""}
			${dots}`;
		el.hidden = false;
		el.querySelectorAll("img").forEach((im) => {
			im.addEventListener("error", () => {
				im.replaceWith(Object.assign(document.createElement("div"), { className: "stylist-noimg" }));
				this.sendSocketNotification("REFRESH_OUTFITS"); // expired link: fetch fresh ones
			}, { once: true });
		});
		if (!this.isOff && o.id) this.sendSocketNotification("SHOWN", { id: o.id });
		if (!this.cycleTimer && this.outfits.length > 1 && this.config.cycleInterval > 0) {
			this.cycleTimer = setInterval(() => {
				if (this.isOff || this.busy()) return;
				this.idx = (this.idx + 1) % this.outfits.length;
				el.classList.add("fading");
				setTimeout(() => {
					this.renderOutfit();
					el.classList.remove("fading");
				}, 800);
			}, this.config.cycleInterval);
		}
	},

	/* ---------- reminders ---------- */

	renderReminders () {
		const el = this.part(".stylist-reminders");
		if (!el) return;
		const list = ((this.today && this.today.reminders) || []).filter((r) => !r.completed).slice(0, this.config.maxReminders);
		if (!this.config.showReminders || !list.length) {
			el.hidden = true;
			return;
		}
		const fmt = (iso) => {
			if (!iso) return "";
			const d = new Date(iso);
			const now = new Date();
			const sameDay = d.toDateString() === now.toDateString();
			const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
			return sameDay ? t : `${d.toLocaleDateString([], { weekday: "short" })} ${t}`;
		};
		const overdue = (iso) => iso && new Date(iso).getTime() < Date.now();
		el.innerHTML = `<div class="stylist-label">Reminders</div><ul>${list
			.map((r) => `<li><span class="t">${this.esc(r.title)}</span>${r.due ? `<span class="due ${overdue(r.due) ? "over" : ""}">${this.esc(fmt(r.due))}</span>` : ""}</li>`)
			.join("")}</ul>`;
		el.hidden = false;
	},

	/* ---------- status ---------- */

	ago (iso) {
		if (!iso) return "never";
		const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
		if (m < 1) return "just now";
		if (m < 60) return `${m} min ago`;
		const h = Math.round(m / 60);
		return h < 36 ? `${h} h ago` : `${Math.round(h / 24)} days ago`;
	},

	renderStatus () {
		const el = this.part(".stylist-status");
		if (!el) return;
		const bits = [];
		if (this.online === false) bits.push(`<span class="warn">● Stylist offline${this.outfits.length ? " · showing earlier data" : ""}</span>`);
		const stale = new Set((this.today && this.today.stale) || []);
		const st = this.status;
		if (st) {
			if (st.weather && st.weather.stale) stale.add("weather");
			Object.entries(st.calendars || {}).forEach(([, c]) => c.configured && c.stale && stale.add("calendar"));
		}
		if (this.today && this.today.date) {
			const local = new Date();
			const todayStr = `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, "0")}-${String(local.getDate()).padStart(2, "0")}`;
			if (this.today.date < todayStr) stale.add("today's plan");
		}
		if (stale.size) bits.push(`<span class="warn">Out of date: ${[...stale].map((s) => this.esc(s)).join(", ")}</span>`);
		const rs = this.today && this.today.last_sync && this.today.last_sync.reminders;
		if (rs) {
			const old = Date.now() - new Date(rs).getTime() > this.config.staleAfterHours * 3600 * 1000;
			bits.push(`<span class="${old ? "warn" : ""}">Reminders synced ${this.esc(this.ago(rs))}</span>`);
		}
		if (this.today && this.today.spend_mode === "blocked") bits.push("<span class=\"warn\">AI budget reached · simple suggestions</span>");
		el.innerHTML = bits.join("<span class=\"sep\"> · </span>");
	},

	/* ---------- pairing ---------- */

	renderPair () {
		const el = this.part(".stylist-pair");
		if (!el) return;
		clearInterval(this.pairTimer);
		this.renderOutfit();
		if (!this.pair) {
			el.hidden = true;
			el.innerHTML = "";
			return;
		}
		const p = this.pair;
		el.innerHTML = `
			<div class="stylist-label">Pair your phone</div>
			<div class="stylist-qr"><img alt="Pairing QR code" src="${this.esc(p.qr)}"></div>
			<div class="stylist-pair-text">Scan with the iPhone Camera app</div>
			<div class="stylist-code">${this.esc(p.code.replace(/(.{4})/, "$1 "))}</div>
			<div class="stylist-pair-exp"></div>`;
		el.hidden = false;
		const exp = el.querySelector(".stylist-pair-exp");
		const tick = () => {
			const s = Math.max(0, Math.round((p.expiresAt - Date.now()) / 1000));
			exp.textContent = `Code expires in ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
			if (s === 0) {
				clearInterval(this.pairTimer);
				if (this.config.pairWhenUnpaired) this.sendSocketNotification("PAIR_REQUEST"); // fresh code
				else {
					this.pair = null;
					this.renderPair();
				}
			}
		};
		tick();
		this.pairTimer = setInterval(tick, 1000);
	},

	/* ---------- capture ---------- */

	renderCapture () {
		this.ensureBodyLayers();
		const c = this.capture || { state: "idle" };
		const s = c.state || "idle";
		// Amber reminder whenever the camera is uncovered outside an active shot.
		const activeShot = ["cover_wait", "preview", "countdown", "flash", "quality_check", "review"].includes(s);
		this.banner.hidden = !((this.coverOpen && !activeShot) || s === "cover_reminder");
		if (s === "idle" || s === "cover_reminder") {
			this.overlay.hidden = true;
			this.overlay.className = "stylist-capture";
			return;
		}
		this.overlay.hidden = false;
		this.overlay.className = `stylist-capture s-${s}`;
		const msg = c.message ? `<div class="stylist-cap-sub">${this.esc(c.message)}</div>` : "";
		const outline = `
			<svg class="stylist-outline" viewBox="0 0 400 520" aria-hidden="true">
				<ellipse cx="200" cy="120" rx="78" ry="96"/>
				<path d="M40 520 C40 360 90 270 200 262 C310 270 360 360 360 520"/>
			</svg>`;
		let html = "";
		switch (s) {
			case "cover_wait":
				html = `<div class="stylist-cap-icon">⬆</div><div class="stylist-cap-title">Slide the camera cover open</div>${msg}`;
				break;
			case "preview":
				html = `${outline}<div class="stylist-cap-title">Stand in the outline</div>${msg}`;
				break;
			case "countdown":
				html = `${outline}<div class="stylist-count">${this.esc(c.seconds_left != null ? c.seconds_left : "")}</div>`;
				break;
			case "flash":
				html = "";
				break;
			case "quality_check":
				html = `<div class="stylist-spinner"></div><div class="stylist-cap-title">Checking the photo…</div>${msg}`;
				break;
			case "review":
				html = `<div class="stylist-review">${c.review_url ? `<img alt="" src="${this.esc(c.review_url)}">` : ""}</div>
					<div class="stylist-cap-title">Keep or retake on your phone</div>${msg}`;
				break;
			default:
				html = `<div class="stylist-cap-title">${this.esc(c.message || "")}</div>`;
		}
		this.overlay.innerHTML = html;
	}
});
