import { Trophy } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Leaderboard() {
  return (
    <Page>
      <PageHeader title="Classement" />
      <EmptyState icon={Trophy} title="Bientôt disponible" description="Classement qualité × robustesse, front de Pareto et heatmap des attaques." />
    </Page>
  );
}
