import { Wallet } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Budget() {
  return (
    <Page>
      <PageHeader title="Budget" />
      <EmptyState icon={Wallet} title="Bientôt disponible" description="Suivi du budget GPU, coûts par projet, machine et mois." />
    </Page>
  );
}
