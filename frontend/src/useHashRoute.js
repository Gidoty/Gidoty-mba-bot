import { useEffect, useState } from "react";

// Minimal `#/route` router - no react-router dependency, matching the
// scholarship app's own frontend convention for a single-handful-of-pages SPA.
export function useHashRoute() {
  const [hash, setHash] = useState(() => window.location.hash.replace(/^#\/?/, "") || "landing");

  useEffect(() => {
    const onChange = () => setHash(window.location.hash.replace(/^#\/?/, "") || "landing");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return hash;
}

export function navigate(route) {
  window.location.hash = `#/${route}`;
}
