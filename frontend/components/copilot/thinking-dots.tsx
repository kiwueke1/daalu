/**
 * Three bouncing dots for "the assistant is working on it".
 *
 * The previous indicator was `animate-shimmer` — a 3.2s fade between 0.6
 * and 1.0 opacity on already-muted grey text. It technically animated, but
 * on a low-contrast label over a three-second cycle it was invisible in
 * practice, and a copilot turn that takes ten seconds or more read as a
 * dead chat. Motion needs to be obvious to function as feedback.
 *
 * Dots are painted in the accent colour rather than muted grey so the eye
 * catches them, and the stagger is a third of the cycle each so the wave
 * reads as travelling rather than pulsing in unison.
 */
export function ThinkingDots({ label }: { label?: string }) {
  return (
    <span
      className="inline-flex items-center gap-2"
      role="status"
      aria-live="polite"
      aria-label={label ?? "Thinking"}
    >
      <span className="inline-flex items-end gap-[3px]" aria-hidden="true">
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            className="h-[5px] w-[5px] rounded-full animate-bounceDot motion-reduce:animate-none"
            style={{
              background: "var(--accent)",
              animationDelay: `${i * 0.16}s`,
            }}
          />
        ))}
      </span>
      {label && <span className="text-muted">{label}</span>}
    </span>
  );
}
