import { useEffect, useRef, useState } from "react";

// A small "i" icon next to a field or button that reveals one short
// explanation on tap - click-based rather than hover-based since this
// has to work on a phone, which is how most customers will use this.
export default function HelpTip({ text }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    function closeIfOutside(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", closeIfOutside);
    document.addEventListener("touchstart", closeIfOutside);
    return () => {
      document.removeEventListener("mousedown", closeIfOutside);
      document.removeEventListener("touchstart", closeIfOutside);
    };
  }, [open]);

  return (
    <span className="relative ml-1 inline-block align-middle" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="More information"
        aria-expanded={open}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 text-[10px] font-semibold leading-none text-slate-500 hover:border-accent hover:text-accent"
      >
        i
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-0 top-full z-20 mt-1 w-60 max-w-[70vw] rounded-md border border-slate-200 bg-white p-2.5 text-xs leading-snug text-slate-600 shadow-lg"
        >
          {text}
        </span>
      )}
    </span>
  );
}
