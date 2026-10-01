import clsx from "clsx";
import { useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, Image as ImageIcon } from "lucide-react";
import { Card, Dialog, EmptyState, IconButton, Switch } from "../../components/ui";
import { fileUrl } from "../../lib/api";
import { formatInt } from "../../lib/format";
import { useLocalStorage } from "../../lib/hooks";
import type { RunDetail, Sample } from "../../lib/types";

/** Preferred column order of sample groups. */
const GROUP_ORDER = ["original", "orig", "input", "watermarked", "wm", "attacked", "att", "diff", "residual", "delta", "frame"];
const GROUP_LABELS: Record<string, string> = {
  original: "Original",
  orig: "Original",
  input: "Original",
  watermarked: "Tatoué",
  wm: "Tatoué",
  attacked: "Attaqué",
  att: "Attaqué",
  diff: "Différence",
  residual: "Résidu",
  delta: "Différence",
  frame: "Image tatouée (vidéo)",
};

const groupRank = (g: string) => {
  const i = GROUP_ORDER.indexOf(g.toLowerCase());
  return i === -1 ? GROUP_ORDER.length : i;
};
const groupLabel = (g: string) => GROUP_LABELS[g.toLowerCase()] ?? g;

/** Item identifier: sample name without its "_<group>" suffix. */
function itemId(s: Sample): string {
  const suffix = `_${s.group}`;
  const base = s.name.replace(/\.[a-z0-9]+$/i, "");
  return base.endsWith(suffix) ? base.slice(0, -suffix.length) : base;
}

function Media({ runId, sample, pixelated, large }: { runId: string; sample: Sample; pixelated: boolean; large?: boolean }) {
  const src = fileUrl(runId, sample.path);
  if (sample.kind === "audio") {
    return <audio controls preload="none" src={src} className="no-drag w-full" />;
  }
  if (sample.kind === "video") {
    return (
      <video
        controls
        loop
        muted
        playsInline
        preload="metadata"
        src={src}
        className={clsx("no-drag w-full rounded-lg bg-black", large ? "max-h-[70vh]" : "aspect-video")}
      />
    );
  }
  return (
    <img
      src={src}
      alt={`${groupLabel(sample.group)} — ${sample.name}`}
      loading="lazy"
      draggable={false}
      className={clsx("w-full rounded-lg bg-surface-2 object-contain", large ? "max-h-[70vh]" : "aspect-square")}
      style={pixelated ? { imageRendering: "pixelated" } : undefined}
    />
  );
}

export function SamplesTab({ run }: { run: RunDetail }) {
  const steps = useMemo(() => [...new Set(run.samples.map((s) => s.step))].sort((a, b) => a - b), [run.samples]);
  const [stepIndex, setStepIndex] = useState<number | null>(null);
  const [pixelated, setPixelated] = useLocalStorage("wmlab.samples.pixelated", true);
  const [lightbox, setLightbox] = useState<string | null>(null);

  const idx = stepIndex === null ? steps.length - 1 : Math.min(stepIndex, steps.length - 1);
  const step = steps[idx];

  const { groups, rows } = useMemo(() => {
    const atStep = run.samples.filter((s) => s.step === step);
    const gs = [...new Set(atStep.map((s) => s.group))].sort((a, b) => groupRank(a) - groupRank(b) || a.localeCompare(b));
    const byItem = new Map<string, Map<string, Sample>>();
    for (const s of atStep) {
      const id = itemId(s);
      if (!byItem.has(id)) byItem.set(id, new Map());
      byItem.get(id)!.set(s.group, s);
    }
    const rs = [...byItem.entries()].sort(([a], [b]) => a.localeCompare(b, "en", { numeric: true }));
    return { groups: gs, rows: rs };
  }, [run.samples, step]);

  if (!steps.length) {
    return (
      <Card padding="none">
        <EmptyState
          icon={ImageIcon}
          title="Aucun échantillon"
          description="Les échantillons (original, tatoué, différence) sont enregistrés tous les samples.every_steps."
        />
      </Card>
    );
  }

  const lightboxRow = rows.find(([id]) => id === lightbox);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-4">
        <div className="flex items-center gap-1">
          <IconButton icon={ChevronLeft} label="Step précédent" size="sm" disabled={idx <= 0} onClick={() => setStepIndex(idx - 1)} />
          <IconButton
            icon={ChevronRight}
            label="Step suivant"
            size="sm"
            disabled={idx >= steps.length - 1}
            onClick={() => setStepIndex(idx + 1)}
          />
        </div>
        <div className="text-[13.5px] text-fg">
          Step <span className="tabular font-medium">{formatInt(step)}</span>
          <span className="text-muted">
            {" "}
            · {idx + 1}/{steps.length}
          </span>
        </div>
        {steps.length > 1 && (
          <input
            type="range"
            min={0}
            max={steps.length - 1}
            value={idx}
            onChange={(e) => setStepIndex(Number(e.target.value))}
            className="no-drag w-64 accent-[var(--accent)]"
            aria-label="Choisir le step"
          />
        )}
        {run.samples.some((s) => s.kind === "image") && (
          <Switch size="sm" checked={pixelated} onChange={setPixelated} label="Pixels nets" className="ml-auto" />
        )}
      </div>

      <div className="overflow-x-auto">
        <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${groups.length}, minmax(140px, ${run.samples.some((s) => s.kind === "image") ? "260px" : "1fr"}))` }}>
          {groups.map((g) => (
            <div key={g} className="text-[12.5px] font-medium text-muted">
              {groupLabel(g)}
            </div>
          ))}
          {rows.map(([id, byGroup]) =>
            groups.map((g) => {
              const s = byGroup.get(g);
              return (
                <div key={`${id}-${g}`} className="min-w-0">
                  {s ? (
                    s.kind === "image" ? (
                      <button type="button" className="no-drag block w-full" onClick={() => setLightbox(id)} title="Agrandir">
                        <Media runId={run.run_id} sample={s} pixelated={pixelated} />
                      </button>
                    ) : (
                      <Media runId={run.run_id} sample={s} pixelated={pixelated} />
                    )
                  ) : (
                    <div className="flex aspect-square items-center justify-center rounded-lg border border-dashed border-border text-[12px] text-faint">
                      —
                    </div>
                  )}
                </div>
              );
            }),
          )}
        </div>
      </div>

      <Dialog open={!!lightboxRow} onClose={() => setLightbox(null)} size="xl" title={`Échantillon ${lightbox ?? ""} · step ${formatInt(step)}`}>
        {lightboxRow && (
          <div className="grid gap-4" style={{ gridTemplateColumns: `repeat(${groups.length}, minmax(0, 1fr))` }}>
            {groups.map((g) => {
              const s = lightboxRow[1].get(g);
              return (
                <div key={g} className="min-w-0">
                  <div className="mb-1.5 text-[12.5px] font-medium text-muted">{groupLabel(g)}</div>
                  {s ? <Media runId={run.run_id} sample={s} pixelated={pixelated} large /> : <div className="text-faint">—</div>}
                </div>
              );
            })}
          </div>
        )}
      </Dialog>
    </div>
  );
}
