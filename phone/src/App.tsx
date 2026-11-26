import { useEffect, useState, useSyncExternalStore } from "react";
import { api, getToken, onTokenChange } from "./api/client";
import { startWs, stopWs } from "./api/ws";
import type { Inbox } from "./api/types";
import { Icon, Toaster } from "./components/ui";
import { useLoad, useOnline, useWsStatus } from "./lib/hooks";
import { match, navigate, usePath } from "./lib/router";
import { init as initUploads, resume as resumeUploads } from "./upload/queue";
import { Pair } from "./screens/Pair";
import { Today } from "./screens/Today";
import { Capture } from "./screens/Capture";
import { AddItems } from "./screens/AddItems";
import { InboxScreen } from "./screens/Inbox";
import { Closet } from "./screens/Closet";
import { ItemDetail } from "./screens/ItemDetail";
import { CheckinScreen } from "./screens/Checkin";
import { Style } from "./screens/Style";
import { Settings } from "./screens/Settings";
import { More } from "./screens/More";

function useToken() {
  return useSyncExternalStore(onTokenChange, getToken);
}

const TABS = [
  { path: "/", label: "Today", icon: Icon.today, match: (p: string) => p === "/" || p.startsWith("/capture") || p.startsWith("/checkin") },
  { path: "/closet", label: "Closet", icon: Icon.closet, match: (p: string) => p.startsWith("/closet") },
  { path: "/add", label: "Add", icon: Icon.add, match: (p: string) => p.startsWith("/add") },
  { path: "/inbox", label: "Inbox", icon: Icon.inbox, match: (p: string) => p.startsWith("/inbox") },
  { path: "/more", label: "More", icon: Icon.more, match: (p: string) => ["/more", "/style", "/settings"].some((x) => p.startsWith(x)) },
];

function TabBar({ path, inboxCount }: { path: string; inboxCount: number }) {
  return (
    <nav className="tabbar" aria-label="Main">
      {TABS.map((t) => {
        const on = t.match(path);
        return (
          <button key={t.path} className={`tab ${on ? "on" : ""}`} aria-current={on ? "page" : undefined} onClick={() => navigate(t.path)}>
            {t.icon}
            <span>{t.label}</span>
            {t.path === "/inbox" && inboxCount > 0 && <span className="badge">{inboxCount > 99 ? "99+" : inboxCount}</span>}
          </button>
        );
      })}
    </nav>
  );
}

function ConnectionPill() {
  const online = useOnline();
  const ws = useWsStatus();
  const [show, setShow] = useState(false);
  // Only complain after a few seconds closed, so brief reconnects don't flash.
  useEffect(() => {
    if (online && ws !== "closed") {
      setShow(false);
      return;
    }
    const t = setTimeout(() => setShow(true), online ? 4000 : 500);
    return () => clearTimeout(t);
  }, [online, ws]);
  if (!show) return null;
  return <div className="offline-pill">{online ? "Reconnecting to the mirror…" : "No connection"}</div>;
}

export function App() {
  const token = useToken();
  const path = usePath();
  const paired = !!token;

  useEffect(() => {
    if (!paired) {
      stopWs();
      if (!path.startsWith("/pair")) navigate("/pair", { replace: true });
      return;
    }
    startWs();
    void initUploads().then(resumeUploads);
  }, [paired, path]);

  const inbox = useLoad(() => (paired ? api<Inbox>("/api/inbox?limit=500") : Promise.resolve({ count: 0, entries: [] })), [paired], ["inbox.updated", "inbox.changed"]);
  const inboxCount = inbox.data?.count ?? 0;

  if (!paired || path.startsWith("/pair")) {
    return (
      <div className="app no-tabs">
        <Pair />
        <Toaster />
      </div>
    );
  }

  let screen: React.ReactNode;
  let m: Record<string, string> | null;
  if (path === "/") screen = <Today />;
  else if (path === "/capture") screen = <Capture />;
  else if (path === "/add") screen = <AddItems />;
  else if (path === "/inbox") screen = <InboxScreen onChanged={inbox.reload} />;
  else if (path === "/closet") screen = <Closet />;
  else if ((m = match("/closet/:id", path))) screen = <ItemDetail id={m.id} />;
  else if ((m = match("/checkin/:id", path))) screen = <CheckinScreen photoId={m.id} />;
  else if (path === "/checkin") screen = <CheckinScreen />;
  else if (path === "/style") screen = <Style />;
  else if (path === "/settings") screen = <Settings />;
  else if (path === "/more") screen = <More />;
  else screen = <Today />;

  return (
    <div className="app">
      <ConnectionPill />
      {screen}
      <TabBar path={path} inboxCount={inboxCount} />
      <Toaster />
    </div>
  );
}
