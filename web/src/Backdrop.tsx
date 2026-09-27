/**
 * Plain canvas: just the theme background. The former metallic-gold corner
 * sheens were removed on 2026-09-02 for the open-source release.
 */
export function Backdrop() {
  return (
    <div
      aria-hidden
      className="pointer-events-none fixed inset-0 -z-10"
      style={{ background: "var(--background-base)" }}
    />
  );
}
