import { Badge, Card, CardHeader, CodeBlock, KeyValue, type KeyValueItem } from "../../components/ui";
import { formatDate, formatInt } from "../../lib/format";
import type { RunDetail } from "../../lib/types";

const s = (v: unknown): string => {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
};

function entries(obj: Record<string, unknown> | null, skip: string[] = []): KeyValueItem[] {
  if (!obj) return [];
  return Object.entries(obj)
    .filter(([k]) => !skip.includes(k))
    .map(([k, v]) => ({ label: k, value: s(v), mono: typeof v !== "string" || /^[\w./:-]+$/.test(String(v)) }));
}

export function EnvTab({ run }: { run: RunDetail }) {
  const { git, git_diff, hardware, command, launch, pip_freeze_lines } = run.env;
  const gpus = (hardware?.gpus as { name?: string; memory_gb?: number }[] | undefined) ?? [];
  const dirty = git?.dirty === true;

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
      <Card>
        <CardHeader
          title="Code (git)"
          description="Capturé sur le Mac au lancement."
          actions={git ? <Badge tone={dirty ? "warning" : "success"}>{dirty ? "modifications non commitées" : "propre"}</Badge> : undefined}
        />
        {git ? (
          <KeyValue
            items={[
              { label: "Commit", value: s(git.sha), mono: true },
              { label: "Branche", value: s(git.branch), mono: true },
              { label: "Remote", value: s(git.remote), mono: true },
            ]}
          />
        ) : (
          <div className="text-[13px] text-muted">Informations git indisponibles (dépôt absent ou non capturé).</div>
        )}
      </Card>

      <Card>
        <CardHeader title="Matériel" />
        {hardware ? (
          <KeyValue
            items={[
              ...entries(hardware, ["gpus"]),
              ...(gpus.length
                ? [{ label: "GPU", value: gpus.map((g) => `${g.name ?? "GPU"}${g.memory_gb ? ` · ${g.memory_gb} Go` : ""}`).join(", ") }]
                : []),
            ]}
          />
        ) : (
          <div className="text-[13px] text-muted">hardware.json absent.</div>
        )}
      </Card>

      <Card className="xl:col-span-2">
        <CardHeader title="Commande" description={`${formatInt(pip_freeze_lines)} paquets figés dans env/pip_freeze.txt`} />
        {command ? <CodeBlock code={command} wrap maxHeight={160} /> : <div className="text-[13px] text-muted">command.txt absent.</div>}
      </Card>

      {launch && (
        <Card className="xl:col-span-2">
          <CardHeader
            title="Lancement depuis l'app"
            description={launch.launched_at ? `Lancé le ${formatDate(String(launch.launched_at))}` : "env/launch.json"}
          />
          <KeyValue columns={2} items={entries(launch, ["git", "git_diff"])} />
        </Card>
      )}

      {git_diff && (
        <Card className="xl:col-span-2" padding="sm">
          <CardHeader title="Diff non commité" description="git diff au moment du lancement." className="mx-2 mt-2" />
          <CodeBlock code={git_diff} maxHeight={520} title="git.diff">
            {git_diff.split("\n").map((l, i) => (
              <div
                key={i}
                className={
                  l.startsWith("+") && !l.startsWith("+++")
                    ? "text-success"
                    : l.startsWith("-") && !l.startsWith("---")
                      ? "text-danger"
                      : l.startsWith("@@")
                        ? "text-info"
                        : undefined
                }
              >
                {l || " "}
              </div>
            ))}
          </CodeBlock>
        </Card>
      )}
    </div>
  );
}
