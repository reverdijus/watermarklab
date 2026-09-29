import { useParams } from "react-router";
import { FlaskConical } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function StudyDetail() {
  const params = useParams();
  const subject = params.id ? decodeURIComponent(params.id) : undefined;
  return (
    <Page>
      <PageHeader title={subject ?? "Étude HPO"} eyebrow={subject ? "Étude HPO" : undefined} />
      <EmptyState icon={FlaskConical} title="Bientôt disponible" description="Essais, importance des paramètres, front de Pareto et coordonnées parallèles." />
    </Page>
  );
}
