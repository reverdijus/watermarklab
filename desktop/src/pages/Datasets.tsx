import { Database } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Datasets() {
  return (
    <Page>
      <PageHeader title="Datasets" />
      <EmptyState icon={Database} title="Bientôt disponible" description="Catalogue des datasets et chemins par machine." />
    </Page>
  );
}
