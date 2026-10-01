/**
 * Read-only summaries of parsed spec YAML (experiment / HPO plan) shown next to the editors.
 * They are best-effort views of the draft: the backend validation stays authoritative.
 */
import type { ReactNode } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { Badge, Table, Tooltip, type Column } from "./ui";
import { searchSpaceRows, type SpaceRow } from "./analysis";
import { formatInt, formatSignificant } from "../lib/format";
import { metricLabel } from "../lib/metrics";

type Dict = Record<string, unknown>;

const isDict = (v: unknown): v is Dict => !!v && typeof v === "object" && !Array.isArray(v);
const asDict = (v: unknown): Dict => (isDict(v) ? v : {});
const asList = (v: unknown): unknown[] => (Array.isArray(v) ? v : []);

/** Short text of any YAML value. */
export function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "number") return formatSignificant(v, 4);
  if (typeof v === "boolean") return v ? "oui" : "non";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return `[${v.map(fmtValue).join(", ")}]`;
  if (isDict(v)) return `{${Object.entries(v).map(([k, x]) => `${k}: ${fmtValue(x)}`).join(", ")}}`;
  return String(v);
}

/** Text of a ScheduledValue (number, ramp or piecewise). */
export function fmtScheduled(v: unknown): string {
  if (typeof v === "number") return formatSignificant(v, 4);
  if (isDict(v) && Array.isArray(v.ramp) && v.ramp.length === 2) {
    const shape = typeof v.shape === "string" && v.shape !== "linear" ? ` (${v.shape})` : "";
    const window =
      (typeof v.start === "number" && v.start !== 0) || (typeof v.end === "number" && v.end !== 1)
        ? ` sur ${Math.round(Number(v.start ?? 0) * 100)}–${Math.round(Number(v.end ?? 1) * 100)} %`
        : "";
    return `${fmtValue(v.ramp[0])} → ${fmtValue(v.ramp[1])}${shape}${window}`;
  }
  if (isDict(v) && Array.isArray(v.piecewise)) return `par morceaux · ${v.piecewise.length} points`;
  return v === undefined ? "—" : fmtValue(v);
}

function ParamChips({ params, max = 8 }: { params: unknown; max?: number }) {
  const entries = Object.entries(asDict(params));
  if (!entries.length) return <span className="text-faint">params par défaut</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {entries.slice(0, max).map(([k, v]) => (
        <span key={k} className="rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-muted">
          {k}={fmtValue(v)}
        </span>
      ))}
      {entries.length > max && <span className="text-[11px] text-faint">+{entries.length - max}</span>}
    </span>
  );
}

function Row({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-3 border-b border-border/70 py-1.5 text-[12.5px] last:border-b-0">
      <div className="text-muted">{label}</div>
      <div className="selectable min-w-0 text-fg">{children}</div>
    </div>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1 text-[11.5px] font-medium uppercase tracking-wide text-faint">{title}</div>
      {children}
    </div>
  );
}

function splitText(split: unknown): ReactNode {
  const d = asDict(split);
  if (!d.dataset) return <span className="text-faint">—</span>;
  const pre = asList(d.preprocess)
    .map((p) => (isDict(p) ? String(p.name ?? "?") : String(p)))
    .filter(Boolean);
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono">{String(d.dataset)}</span>
        <ParamChips params={d.params} max={5} />
      </div>
      {(pre.length > 0 || d.limit != null) && (
        <div className="text-[11.5px] text-muted">
          {pre.length ? `prétraitements : ${pre.join(" → ")}` : ""}
          {d.limit != null ? `${pre.length ? " · " : ""}limite ${fmtValue(d.limit)}` : ""}
        </div>
      )}
    </div>
  );
}

function attackChips(attacks: unknown): ReactNode {
  const list = asList(attacks);
  if (!list.length) return <span className="text-faint">aucune (identité)</span>;
  return (
    <span className="inline-flex flex-wrap gap-1">
      {list.map((a, i) => {
        const d = asDict(a);
        const label = String(d.label ?? d.name ?? "?");
        const p = typeof d.p === "number" && d.p !== 1 ? ` · p=${fmtValue(d.p)}` : "";
        return (
          <Tooltip key={i} content={<span className="font-mono">{`${String(d.name ?? "?")} ${fmtValue(d.params ?? {})}`}</span>}>
            <Badge tone="neutral">{`${label}${p}`}</Badge>
          </Tooltip>
        );
      })}
    </span>
  );
}

/** Summary of an experiment spec: model, data, optimisation, phases and evaluation. */
export function ExperimentSummary({ spec }: { spec: Dict | null }) {
  if (!spec) return <div className="text-[13px] text-muted">YAML illisible : le résumé réapparaîtra une fois la syntaxe corrigée.</div>;
  const model = asDict(spec.model);
  const data = asDict(spec.data);
  const opt = asDict(spec.optimizer);
  const terms = asDict(asDict(spec.loss).terms);
  const phases = asList(spec.phases);
  const evalSpec = asDict(spec.eval);
  const initFrom = isDict(spec.init_from) ? spec.init_from : null;
  const final = isDict(evalSpec.final_benchmark) ? evalSpec.final_benchmark : null;
  return (
    <div className="space-y-4">
      <Group title="Modèle">
        <Row label="Architecture">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono">{String(model.name ?? "—")}</span>
            <ParamChips params={model.params} />
          </div>
        </Row>
        {initFrom && (
          <Row label="Initialisation">
            <span className="font-mono text-[12px]">
              {String(initFrom.run ?? "?")} · {String(initFrom.checkpoint ?? "last")}
              {initFrom.resume === true ? " · reprise au step du checkpoint" : initFrom.start_phase != null ? ` → ${String(initFrom.start_phase)}` : ""}
            </span>
          </Row>
        )}
      </Group>
      <Group title="Données">
        <Row label="Entraînement">{splitText(data.train)}</Row>
        <Row label="Validation">{splitText(data.val)}</Row>
        <Row label="Batch">
          {fmtValue(data.batch_size ?? "—")}
          {data.num_workers != null && <span className="text-muted"> · {fmtValue(data.num_workers)} workers</span>}
        </Row>
      </Group>
      <Group title="Optimisation">
        <Row label="Optimiseur">
          <span className="font-mono">{String(opt.name ?? "—")}</span>
          <span className="text-muted">
            {" "}
            · lr {fmtValue(opt.lr)}
            {opt.weight_decay != null ? ` · wd ${fmtValue(opt.weight_decay)}` : ""}
            {opt.grad_clip != null ? ` · clip ${fmtValue(opt.grad_clip)}` : ""}
          </span>
        </Row>
        <Row label="Pertes">
          <span className="inline-flex flex-wrap gap-x-3 gap-y-1">
            {Object.entries(terms).map(([k, t]) => (
              <span key={k}>
                <span className="font-mono">{k}</span>
                <span className="text-muted">
                  {" "}
                  {String(asDict(t).name ?? "?")} × {fmtValue(asDict(t).weight ?? 1)}
                </span>
              </span>
            ))}
            {!Object.keys(terms).length && <span className="text-faint">—</span>}
          </span>
        </Row>
      </Group>
      <Group title={`Phases (${phases.length})`}>
        <ol className="space-y-2">
          {phases.map((raw, i) => {
            const p = asDict(raw);
            const lr = asDict(p.lr);
            const weights = Object.entries(asDict(p.loss_weights));
            return (
              <li key={i} className="rounded-lg border border-border px-3 py-2">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-[13px] font-medium text-fg">
                    <span className="mr-1.5 text-faint">{i + 1}.</span>
                    {String(p.name ?? `phase ${i + 1}`)}
                  </span>
                  <span className="tabular shrink-0 text-[12px] text-muted">
                    {typeof p.steps === "number" ? `${formatInt(p.steps)} steps` : "—"}
                  </span>
                </div>
                <div className="mt-1.5 space-y-1 text-[12px] text-muted">
                  <div>
                    LR : <span className="text-fg">{String(lr.schedule ?? "constant")}</span>
                    {typeof lr.warmup_steps === "number" && lr.warmup_steps > 0 ? ` · warmup ${formatInt(lr.warmup_steps)}` : ""}
                    {lr.factor != null && lr.factor !== 1 ? ` · ×${fmtValue(lr.factor)}` : ""}
                    {p.batch_size != null ? ` · batch ${fmtValue(p.batch_size)}` : ""}
                    {p.reset_optimizer ? " · optimiseur réinitialisé" : ""}
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    Attaques : {attackChips(p.attacks)}
                    {asList(p.attacks).length > 0 && (
                      <span>
                        · force <span className="text-fg">{fmtScheduled(p.attack_strength ?? 1)}</span>
                        {p.random_strength === false ? " (fixe)" : ""}
                      </span>
                    )}
                  </div>
                  {weights.length > 0 && (
                    <div>
                      Poids :{" "}
                      {weights.map(([k, v], j) => (
                        <span key={k}>
                          {j > 0 && " · "}
                          <span className="font-mono">{k}</span> <span className="text-fg">{fmtScheduled(v)}</span>
                        </span>
                      ))}
                    </div>
                  )}
                  {asList(p.freeze).length > 0 && <div>Gelé : {asList(p.freeze).map(String).join(", ")}</div>}
                  {isDict(p.data) && <div>Données remplacées pour cette phase</div>}
                </div>
              </li>
            );
          })}
          {!phases.length && <li className="text-[12.5px] text-faint">Aucune phase</li>}
        </ol>
      </Group>
      <Group title="Évaluation">
        <Row label="Validation">
          {evalSpec.every_steps != null ? `toutes les ${formatInt(Number(evalSpec.every_steps))} steps` : "—"}
          {evalSpec.val_batches != null && <span className="text-muted"> · {fmtValue(evalSpec.val_batches)} batches</span>}
        </Row>
        <Row label="Attaques val.">{attackChips(evalSpec.val_attacks)}</Row>
        <Row label="Benchmark final">
          {final ? (
            <span>
              <span className="font-mono">{String(final.benchmark ?? "?")}</span>
              <span className="text-muted"> · {asList(final.checkpoints).map(String).join(", ") || "last"}</span>
            </span>
          ) : (
            <span className="text-faint">aucun</span>
          )}
        </Row>
      </Group>
    </div>
  );
}

// ---------------------------------------------------------------------------
// HPO plan
// ---------------------------------------------------------------------------

/** Search space table of an HPO plan. */
export function SearchSpaceTable({ space }: { space: unknown }) {
  const rows = searchSpaceRows(space);
  const columns: Column<SpaceRow>[] = [
    {
      id: "param",
      header: "Paramètre",
      // Flexible column: long dotted paths truncate instead of widening the table.
      className: "w-full max-w-0",
      headerClassName: "w-full",
      cell: (r) => (
        <span className="block truncate font-mono text-[12px] text-fg" title={r.param}>
          {r.param}
        </span>
      ),
      sortValue: (r) => r.param,
    },
    { id: "type", header: "Type", cell: (r) => <span className="text-muted">{r.type}</span>, sortValue: (r) => r.type },
    { id: "bounds", header: "Bornes", cell: (r) => <span className="tabular">{r.bounds}</span> },
    { id: "detail", header: "Log / choix", wrap: true, cell: (r) => <span className="text-muted">{r.detail}</span> },
  ];
  return <Table bare dense columns={columns} rows={rows} rowKey={(r) => r.param} empty="Espace de recherche vide." />;
}

/** Objectives and constraints of an HPO plan. */
export function HpoObjectives({ plan }: { plan: Dict }) {
  const objectives = asList(plan.objectives).map(asDict);
  const constraints = asList(plan.constraints).map(asDict);
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {objectives.map((o, i) => {
          const Arrow = o.direction === "minimize" ? ArrowDown : ArrowUp;
          return (
            <Badge key={i} tone="accent" size="md" icon={Arrow} title={String(o.metric ?? "")}>
              {metricLabel(String(o.metric ?? "?"), { withGroup: true })}
            </Badge>
          );
        })}
        {objectives.length > 1 && (
          <Badge tone="info" size="md">
            multi-objectif · pas d'élagage
          </Badge>
        )}
        {!objectives.length && <span className="text-[12.5px] text-faint">Aucun objectif</span>}
      </div>
      {constraints.length > 0 && (
        <div className="text-[12.5px] text-muted">
          Contraintes :{" "}
          {constraints.map((c, i) => (
            <span key={i} className="text-fg">
              {i > 0 && " · "}
              {metricLabel(String(c.metric ?? "?"), { withGroup: true })} {String(c.op ?? "").replace(">=", "≥").replace("<=", "≤")}{" "}
              {fmtValue(c.value)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** Sampler, pruner, fidelity and aggregation of an HPO plan. */
export function HpoStrategy({ plan }: { plan: Dict }) {
  const sampler = asDict(plan.sampler);
  const pruner = asDict(plan.pruner);
  const fidelity = asDict(plan.fidelity);
  const overrides = Object.entries(asDict(fidelity.overrides));
  return (
    <div>
      <Row label="Sampler">
        <span className="font-mono">{String(sampler.name ?? "tpe")}</span> <ParamChips params={sampler.params} max={4} />
      </Row>
      <Row label="Pruner">
        <span className="font-mono">{String(pruner.name ?? "none")}</span> <ParamChips params={pruner.params} max={4} />
      </Row>
      <Row label="Fidélité">
        {typeof fidelity.steps_scale === "number" ? `${Math.round(fidelity.steps_scale * 100)} % des steps` : "100 % des steps"}
        {overrides.length > 0 && (
          <div className="mt-1 flex flex-wrap gap-1">
            {overrides.map(([k, v]) => (
              <span key={k} className="rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-muted">
                {k}={fmtValue(v)}
              </span>
            ))}
          </div>
        )}
      </Row>
      <Row label="Agrégation">{plan.objective_agg === "best" ? "meilleure valeur vue" : "valeur finale de l'essai"}</Row>
    </div>
  );
}

export { asDict, asList, isDict };
