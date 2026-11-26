import { navigate } from "../lib/router";

export function More() {
  const rows: [string, string, string][] = [
    ["📷", "Mirror photo", "/capture"],
    ["🧍", "Check in what I'm wearing", "/checkin"],
    ["✨", "Style profile", "/style"],
    ["⚙️", "Settings", "/settings"],
  ];
  return (
    <div className="page">
      <h1 className="large-title">More</h1>
      <div className="list mt">
        {rows.map(([icon, text, to]) => (
          <button key={to} className="list-row chev" onClick={() => navigate(to)}>
            <span aria-hidden style={{ width: 28, textAlign: "center" }}>
              {icon}
            </span>
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}
