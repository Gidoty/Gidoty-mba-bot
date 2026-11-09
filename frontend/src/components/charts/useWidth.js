import { useLayoutEffect, useState } from "react";

// Width of an element in pixels, kept current. Charts draw at this width, so
// their text stays the same size on a phone and on a desktop instead of
// scaling down with the drawing.
export default function useWidth(ref, fallback = 640) {
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    const read = () => setWidth(Math.max(240, Math.floor(el.getBoundingClientRect().width)));
    read();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(read);
    observer.observe(el);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}
