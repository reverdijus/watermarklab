import { GitCompareArrows } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Compare() {
  return (
    <Page>
      <PageHeader title="Comparer des runs" />
      <EmptyState icon={GitCompareArrows} title="Bientôt disponible" description="Courbes superposées, paramètres et résumés côte à côte." />
    </Page>
  );
}
