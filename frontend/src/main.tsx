import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { StyleguideView } from "./StyleguideView";
import "./fonts.css";
import "./index.css";

// Tiny path-based routing — the only route we mount that isn't App is
// the glimmung styleguide pilot's /_styleguide visual catalog. Avoids
// pulling in react-router for this single split.
function Root() {
  if (typeof window !== "undefined" && window.location.pathname === "/_styleguide") {
    return <StyleguideView />;
  }
  return <App />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
