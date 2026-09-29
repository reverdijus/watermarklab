import { useParams } from "react-router";
import { SquarePen } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function ExperimentEditor() {
  const params = useParams();
  const subject = params.exp ? decodeURIComponent(params.exp) : undefined;
  return (
    <Page>
      <PageHeader title={subject ?? "Expérience"} eyebrow={subject ? "Expérience" : undefined} />
      <EmptyState icon={SquarePen} title="Bientôt disponible" description="Éditeur YAML, timeline des phases, validation et lancement." />
    </Page>
  );
}
