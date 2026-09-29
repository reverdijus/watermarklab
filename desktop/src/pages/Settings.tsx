import { Settings } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Settings() {
  return (
    <Page>
      <PageHeader title="Réglages" />
      <EmptyState icon={Settings} title="Bientôt disponible" description="Réglages du workspace et de l'application." />
    </Page>
  );
}
