import { useParams } from "react-router";
import { FlaskConical } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function HpoPlanEditor() {
  const params = useParams();
  const subject = params.plan ? decodeURIComponent(params.plan) : undefined;
  return (
    <Page>
      <PageHeader title={subject ?? "Plan HPO"} eyebrow={subject ? "Plan HPO" : undefined} />
      <EmptyState icon={FlaskConical} title="Bientôt disponible" description="Éditeur du plan d'optimisation d'hyperparamètres." />
    </Page>
  );
}
