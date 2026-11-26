/* Example MagicMirror² config/config.js for the bathroom mirror (portrait 1080x1920).
 *
 * Replace 8000 with the port mirror-api listens on (API_PORT), and set the weather
 * location to yours. Copy what you need into ~/MagicMirror/config/config.js.
 */
let config = {
	address: "localhost", // the screen is on the Pi itself; nothing else needs MagicMirror
	port: 8080,
	ipWhitelist: ["127.0.0.1", "::ffff:127.0.0.1", "::1"],
	language: "en",
	locale: "en-US",
	timeFormat: 12,
	units: "imperial",

	modules: [
		{ module: "clock", position: "top_left" },
		{
			// Stock weather module: keeps working even if mirror-api is down.
			module: "weather",
			position: "top_right",
			config: {
				weatherProvider: "openmeteo",
				type: "current",
				lat: 0.0, // your latitude
				lon: 0.0 // your longitude
			}
		},
		{
			// Stock calendar module, pointed at mirror-api's merged feed (localhost only).
			// The three iCal URLs stay in mirror-api's .env; MagicMirror never sees them.
			module: "calendar",
			header: "Today",
			position: "top_left",
			config: {
				maximumEntries: 6,
				fetchInterval: 5 * 60 * 1000,
				calendars: [
					{ url: "http://127.0.0.1:8000/api/calendar.ics", symbol: "calendar" }
				]
			}
		},
		{
			module: "MMM-Stylist",
			position: "lower_third",
			config: {
				apiPort: 8000,
				// What the pairing QR opens on the phone. Leave null to let mirror-api use
				// the Pi's LAN address; set it if that guess is wrong (e.g. two network cards).
				phoneBaseUrl: null, // e.g. "http://192.168.1.20:8000"
				offDelay: 3 * 60 * 1000, // blank the mirror 3 min after the last presence
				cycleInterval: 25 * 1000,
				maxReminders: 5
			}
		}
	]
};

/*************** DO NOT EDIT THE LINE BELOW ***************/
if (typeof module !== "undefined") { module.exports = config; }
