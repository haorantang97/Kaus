/** A retired integration remains readable and manageable, but is not a new target. */
export function engineAcceptsNewWork(entry?: {
  catalogStatus?: string;
  preset?: { catalogStatus?: string };
} | null): boolean {
  return (entry?.catalogStatus ?? entry?.preset?.catalogStatus) !== "legacy";
}
