import { useSyncExternalStore } from "react";

// Colour theme: "auto" follows the device, "light" and "dark" are explicit.
// The choice is remembered per browser. The page always carries the resolved
// theme in data-theme ("light" or "dark") so the CSS variables in tokens.css
// have one selector to match; the raw choice is kept in data-theme-choice.
const KEY = "mba-theme";
export const THEME_CHOICES = ["auto", "light", "dark"];

const listeners = new Set();

export function getThemeChoice() {
  let stored = null;
  try {
    stored = window.localStorage.getItem(KEY);
  } catch {
    // Private window or blocked storage: fall back to "auto".
  }
  return THEME_CHOICES.includes(stored) ? stored : "auto";
}

export function resolveTheme(choice) {
  if (choice === "light" || choice === "dark") return choice;
  const dark = typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
  return dark ? "dark" : "light";
}

export function applyTheme(choice = getThemeChoice()) {
  const root = document.documentElement;
  root.setAttribute("data-theme", resolveTheme(choice));
  root.setAttribute("data-theme-choice", choice);
}

export function setThemeChoice(choice) {
  if (!THEME_CHOICES.includes(choice)) return;
  try {
    window.localStorage.setItem(KEY, choice);
  } catch {
    // The choice still applies for this visit.
  }
  applyTheme(choice);
  listeners.forEach((fn) => fn());
}

// Call once before the first render. Also follows device changes while "auto".
export function initTheme() {
  applyTheme();
  if (typeof window.matchMedia === "function") {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (getThemeChoice() === "auto") {
        applyTheme("auto");
        listeners.forEach((fn) => fn());
      }
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  }
}

function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// [choice, setChoice]
export function useThemeChoice() {
  const choice = useSyncExternalStore(subscribe, getThemeChoice, () => "auto");
  return [choice, setThemeChoice];
}
