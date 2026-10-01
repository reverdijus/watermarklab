import { useSearchParams } from "react-router";
import { Info } from "lucide-react";
import { Card, Page, PageHeader, Select, Skeleton } from "../components/ui";
import { LeaderboardView } from "../components/leaderboard/LeaderboardView";
import { useProjects } from "../lib/queries";
import { MODALITY_LABELS } from "../lib/runs";

function ParetoHelp() {
  return (
    <Card muted padding="md" className="mb-6">
      <div className="flex gap-3">
        <Info size={17} strokeWidth={1.75} className="mt-0.5 shrink-0 text-info" aria-hidden />
        <div className="grid flex-1 grid-cols-1 gap-x-8 gap-y-2 text-[13px] leading-relaxed text-muted lg:grid-cols-3">
          <p>
            <span className="font-medium text-fg">Deux objectifs en tension.</span> Un watermark plus fort résiste mieux aux attaques
            (robustesse ↑) mais se voit ou s'entend davantage (qualité ↓). Il n'existe donc pas « un » meilleur modèle.
          </p>
          <p>
            <span className="font-medium text-fg">Le front de Pareto</span> relie les points non dominés : aucun autre point n'est à la fois
            plus imperceptible et plus robuste. Un point sous le front est battu sur les deux axes par au moins un autre.
          </p>
          <p>
            <span className="font-medium text-fg">Choisir un point.</span> Fixez un seuil de qualité acceptable (curseur de contrainte) :
            le meilleur candidat est le point le plus robuste parmi ceux qui le respectent.
          </p>
        </div>
      </div>
    </Card>
  );
}

export default function Leaderboard() {
  const [params, setParams] = useSearchParams();
  const projects = useProjects();
  const projectName = params.get("project") ?? "";
  const project = projects.data?.find((p) => p.name === projectName) ?? null;

  const setProject = (name: string) => {
    const next = new URLSearchParams();
    if (name) next.set("project", name);
    const source = params.get("source");
    if (source) next.set("source", source);
    setParams(next, { replace: true });
  };

  return (
    <Page width="wide">
      <PageHeader
        title="Classement"
        description="Compromis imperceptibilité × robustesse de tous les checkpoints évalués, tous projets confondus."
        actions={
          <Select
            size="md"
            value={projectName}
            onChange={setProject}
            placeholder="Tous les projets"
            options={(projects.data ?? []).map((p) => ({
              value: p.name,
              label: `${p.title || p.name} · ${MODALITY_LABELS[p.modality] ?? p.modality}`,
            }))}
            className="w-72"
          />
        }
      />
      <ParetoHelp />
      {projectName && projects.isLoading ? (
        <Skeleton className="h-[420px] rounded-xl" />
      ) : (
        <LeaderboardView key={projectName || "all"} project={project} groupByProject={!project} />
      )}
    </Page>
  );
}
