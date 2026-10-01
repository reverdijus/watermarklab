import type { ReactNode } from "react";
import { Link } from "react-router";
import { Card, CardHeader, KeyValue } from "../../components/ui";
import { YamlEditor } from "../../components/YamlEditor";
import { experimentPath } from "../../lib/runs";
import type { RunDetail } from "../../lib/types";

const fmtParam = (v: string | number | boolean | null) => (v === null ? "null" : typeof v === "boolean" ? (v ? "true" : "false") : String(v));

/**
 * Resolved spec, overrides, dataset versions and flattened params. `restricted` (member, §9):
 * the server sends an empty spec, no overrides and no params — `restrictedNotice` replaces them.
 */
export function ConfigTab({ run, restricted = false, restrictedNotice }: { run: RunDetail; restricted?: boolean; restrictedNotice?: ReactNode }) {
  const overrides = Object.entries(run.overrides ?? {});
  const datasets = Object.entries(run.datasets ?? {});
  const params = Object.entries(run.params ?? {}).sort(([a], [b]) => a.localeCompare(b));
  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_380px]">
      {restricted ? (
        <Card padding="none" className="self-start">
          {restrictedNotice}
        </Card>
      ) : (
      <div className="min-w-0">
        <div className="mb-2 flex items-baseline justify-between gap-3">
          <div className="text-[13.5px] font-medium text-fg">Spec résolue (spec.yaml)</div>
          {run.spec_path && (
            <Link to={experimentPath(run.project, run.experiment)} className="no-drag truncate font-mono text-[12px] text-muted hover:text-accent">
              {run.spec_path}
            </Link>
          )}
        </div>
        <YamlEditor value={run.spec_yaml || "# spec.yaml introuvable"} readOnly maxHeight="calc(100vh - 300px)" />
      </div>
      )}
      <div className="space-y-4">
        {!restricted && (
          <Card>
            <CardHeader title="Overrides" description="Valeurs passées au lancement (--set)." />
            {overrides.length ? (
              <KeyValue dense items={overrides.map(([k, v]) => ({ label: <span className="font-mono text-[12px]">{k}</span>, value: v, mono: true }))} />
            ) : (
              <div className="text-[13px] text-muted">Aucun override.</div>
            )}
          </Card>
        )}
        <Card>
          <CardHeader title="Versions des datasets" description="Empreinte de la définition de chaque split." />
          {datasets.length ? (
            <KeyValue dense items={datasets.map(([k, v]) => ({ label: k, value: v, mono: true }))} />
          ) : (
            <div className="text-[13px] text-muted">Non renseignées.</div>
          )}
        </Card>
        {!restricted && (
        <Card>
          <CardHeader title="Paramètres" description="Scalaires aplatis de la spec." />
          {params.length ? (
            <KeyValue
              dense
              items={params.map(([k, v]) => ({ label: <span className="font-mono text-[12px]">{k}</span>, value: fmtParam(v), mono: true }))}
            />
          ) : (
            <div className="text-[13px] text-muted">Aucun paramètre.</div>
          )}
        </Card>
        )}
      </div>
    </div>
  );
}
