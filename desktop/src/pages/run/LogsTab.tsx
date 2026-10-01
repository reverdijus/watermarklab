import clsx from "clsx";
import { useEffect, useMemo, useRef, useState } from "react";
import { Copy, Search, Terminal } from "lucide-react";
import { Button, Card, EmptyState, Input, LoadingBlock, Select, Spinner, Switch, copyText, useToast } from "../../components/ui";
import { errorMessage } from "../../lib/api";
import { plural } from "../../lib/format";
import { useRunLogs } from "../../lib/queries";
import type { RunDetail } from "../../lib/types";

function lineClass(line: string): string | undefined {
  if (/(traceback|error|exception|failed|erreur)/i.test(line)) return "text-danger";
  if (/(warn|avertissement)/i.test(line)) return "text-warning";
  return undefined;
}

export function LogsTab({ run, live }: { run: RunDetail; live: boolean }) {
  const [tail, setTail] = useState("500");
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const logs = useRunLogs(run.run_id, { tail: Number(tail), live });
  const toast = useToast();
  const scrollRef = useRef<HTMLDivElement>(null);

  const lines = useMemo(() => {
    const all = logs.data?.lines ?? [];
    const q = filter.trim().toLowerCase();
    return q ? all.filter((l) => l.toLowerCase().includes(q)) : all;
  }, [logs.data, filter]);

  useEffect(() => {
    if (follow && scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [lines, follow]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (!atBottom && follow) setFollow(false);
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Input icon={Search} size="sm" placeholder="Filtrer les lignes" value={filter} onChange={(e) => setFilter(e.target.value)} className="w-64" />
        <Select
          size="sm"
          value={tail}
          onChange={setTail}
          options={["200", "500", "2000", "5000"].map((v) => ({ value: v, label: `${v} dernières lignes` }))}
          className="w-48"
        />
        <Switch size="sm" checked={follow} onChange={setFollow} label="Suivre" />
        {live && logs.isFetching && <Spinner size={14} />}
        <span className="ml-auto text-[12px] text-muted">{plural(lines.length, "ligne")}</span>
        <Button
          size="sm"
          icon={Copy}
          onClick={() => void copyText(lines.join("\n")).then((ok) => ok && toast.info("Logs copiés"))}
          disabled={!lines.length}
        >
          Copier
        </Button>
      </div>
      {logs.isLoading ? (
        <LoadingBlock label="Chargement des logs…" />
      ) : logs.isError ? (
        <Card padding="none">
          <EmptyState compact icon={Terminal} title="Logs indisponibles" description={errorMessage(logs.error)} />
        </Card>
      ) : !lines.length ? (
        <Card padding="none">
          <EmptyState compact icon={Terminal} title={filter ? "Aucune ligne ne correspond" : "stdout.log est vide"} />
        </Card>
      ) : (
        <div
          ref={scrollRef}
          onScroll={onScroll}
          className="selectable h-[calc(100vh-330px)] min-h-[320px] overflow-auto rounded-xl border border-border bg-surface-2 px-4 py-3 font-mono text-[12px] leading-[1.6]"
        >
          {lines.map((l, i) => (
            <div key={i} className={clsx("whitespace-pre-wrap break-all", lineClass(l) ?? "text-fg")}>
              {l || " "}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
