import { Bot } from "lucide-react";
import { engineBrand } from "../lib/backend-display";

/** Shared, decorative engine mark. The surrounding control owns its label. */
export function EngineIcon({ engine, size = 16 }: { engine: string | null | undefined; size?: number }) {
  const brand = engineBrand(engine);
  const icon = brand?.icon;
  // Pi and Oh My Pi already contain their original colors in the base asset.
  const variant = brand?.color && icon !== "pi" && icon !== "omp" ? "-color" : "";
  const src = icon ? `/agent-icons/${icon}${variant}.svg` : null;
  if (brand?.color && src) return <img src={src} width={size} height={size} alt="" className="kaus-engine-icon" data-engine-icon={icon} />;
  return <span className="kaus-engine-icon kaus-engine-icon--tile" aria-hidden="true" data-engine-icon={icon ?? "generic"}
    style={{ width: size, height: size, padding: size * 0.1, borderRadius: size * 0.22, backgroundColor: brand?.background, color: brand?.foreground }}>
    {src
      ? <span className="kaus-engine-icon-mask" style={{ maskImage: `url("${src}")`, WebkitMaskImage: `url("${src}")` }} />
      : <Bot size="100%" />}
  </span>;
}
