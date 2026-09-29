import { useParams } from "react-router";
import { FolderKanban } from "lucide-react";
import { EmptyState, Page, PageHeader } from "../components/ui";

/** Placeholder — implemented by DESKTOP-B. */
export default function ProjectPage() {
  const params = useParams();
  const subject = params.name ? decodeURIComponent(params.name) : undefined;
  return (
    <Page>
      <PageHeader title={subject ?? "Projet"} eyebrow={subject ? "Projet" : undefined} />
      <EmptyState icon={FolderKanban} title="Bientôt disponible" description="Vue d'ensemble, expériences, runs, études HPO et classement du projet." />
    </Page>
  );
}
