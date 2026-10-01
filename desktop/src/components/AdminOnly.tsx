/**
 * Read-only interface of a member (§12.6): pages and blocks reserved to administrators.
 * `RequireCode` wraps a route that shows code (YAML editors, registries): for a member it
 * renders « Réservé aux administrateurs » without mounting the page (no `/api/files`,
 * `/api/registry` request).
 */
import type { ReactNode } from "react";
import { useParams } from "react-router";
import { Lock } from "lucide-react";
import { Button, Card, EmptyState, Page, PageSkeleton } from "./ui";
import { usePermissions } from "../lib/queries";
import { projectPath } from "../lib/runs";

/** Message shown in place of an admin-only block (compact) or page. */
export function AdminOnlyNotice({ what, compact, action }: { what?: string; compact?: boolean; action?: ReactNode }) {
  return (
    <EmptyState
      compact={compact}
      icon={Lock}
      title="Réservé aux administrateurs"
      description={what ?? "Votre compte membre donne accès au suivi des runs, des études, du classement et du budget, en lecture seule."}
      action={action}
    />
  );
}

export function RequireCode({ children, what }: { children: ReactNode; what?: string }) {
  const perms = usePermissions();
  const params = useParams();
  if (perms.canViewCode) return <>{children}</>;
  if (perms.loading) return <PageSkeleton />;
  const project = params.name ? decodeURIComponent(params.name) : null;
  return (
    <Page>
      <Card padding="none">
        <AdminOnlyNotice
          what={what}
          action={project ? <Button to={projectPath(project, "experiments")}>Retour au projet</Button> : <Button to="/">Accueil</Button>}
        />
      </Card>
    </Page>
  );
}
