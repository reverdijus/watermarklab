import { Server } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function Hosts() {
  return (
    <Page>
      <PageHeader title="Machines" />
      <EmptyState icon={Server} title="Bientôt disponible" description="Machines locales et serveurs GPU (SSH), tests de connexion et préparation." />
    </Page>
  );
}
