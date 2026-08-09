import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initializeTelegram } from "./telegram";

initializeTelegram();
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);
