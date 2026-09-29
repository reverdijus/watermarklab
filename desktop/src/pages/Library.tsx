import { Library } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Library() {
  return (
    <Page>
      <PageHeader title="Bibliothèque" />
      <EmptyState icon={Library} title="Bientôt disponible" description="Architectures, attaques, datasets, pertes et métriques enregistrés." />
    </Page>
  );
}
