import { useState, type ReactNode } from "react";
import { CircleAlert, Lock, Save } from "lucide-react";
import { Button, Callout, Drawer, LoadingBlock, Switch, useConfirm, useToast } from "./ui";
import { YamlEditor } from "./YamlEditor";
import { toastFileSaved } from "./saveToast";
import { useSpecDraft } from "./useSpecDraft";
import { errorMessage } from "../lib/api";

export interface YamlFileDrawerProps {
  open: boolean;
  onClose: () => void;
  /** Workspace-relative path (e.g. "datasets/coco2017.yaml"). */
  path: string | null;
  /** Spec kind used for live validation ("dataset", "benchmark", "project"…). */
  kind?: string;
  title: ReactNode;
  description?: ReactNode;
  /** Read-only until the user explicitly unlocks editing (published benchmarks). */
  locked?: boolean;
  /**
   * Whether a locked file may be unlocked at all (default true). False for a published
   * benchmark already referenced by evaluations: the file stays read-only and `lockAction`
   * (e.g. "Nouvelle version") is the only way forward.
   */
  unlockable?: boolean;
  /** Message shown above the editor while locked / when unlocked. */
  lockMessage?: ReactNode;
  /** Action offered in the lock notice (e.g. a "Nouvelle version" button). */
  lockAction?: ReactNode;
  /** Extra content above the editor (help, links…). */
  children?: ReactNode;
  width?: number;
  onSaved?: () => void;
}

/** Right-side YAML editor for one workspace file (live validation, ⌘S, unsaved guard). */
export function YamlFileDrawer(props: YamlFileDrawerProps) {
  if (!props.open || !props.path) return null;
  // Keyed by path so each file gets a fresh draft.
  return <DrawerBody key={props.path} {...props} path={props.path} />;
}

function DrawerBody({
  onClose,
  path,
  kind,
  title,
  description,
  locked = false,
  unlockable = true,
  lockMessage,
  lockAction,
  children,
  width = 760,
  onSaved,
}: YamlFileDrawerProps & { path: string }) {
  const spec = useSpecDraft(path, kind);
  const confirm = useConfirm();
  const toast = useToast();
  const [unlocked, setUnlocked] = useState(!locked);
  const readOnly = !unlocked || (locked && !unlockable);

  const close = async () => {
    if (spec.dirty) {
      const ok = await confirm({
        title: "Abandonner les modifications ?",
        message: "Les changements non enregistrés de ce fichier seront perdus.",
        confirmLabel: "Abandonner",
        cancelLabel: "Continuer l'édition",
        tone: "danger",
      });
      if (!ok) return;
      spec.reset();
    }
    onClose();
  };

  const save = async () => {
    if (readOnly || !spec.dirty) return;
    try {
      const res = await spec.save();
      if (res.validation.valid) toastFileSaved(toast, res, "Fichier enregistré", path);
      else toastFileSaved(toast, res, "Enregistré avec des erreurs", `${res.validation.errors.length} erreur(s) de validation à corriger.`, { forceWarning: true });
      onSaved?.();
    } catch (err) {
      toast.error("Enregistrement impossible", errorMessage(err));
    }
  };

  return (
    <Drawer
      open
      onClose={() => void close()}
      width={width}
      title={title}
      description={description ?? <span className="font-mono text-[12px]">{path}</span>}
      footer={
        <>
          <span className="mr-auto text-[12.5px] text-muted">
            {spec.dirty ? (
              <span className="inline-flex items-center gap-1.5 text-warning">
                <span className="h-1.5 w-1.5 rounded-full bg-warning" /> Modifications non enregistrées
              </span>
            ) : readOnly ? (
              <span className="inline-flex items-center gap-1.5">
                <Lock size={13} strokeWidth={1.75} aria-hidden /> Lecture seule
              </span>
            ) : (
              "À jour"
            )}
          </span>
          <Button variant="ghost" onClick={() => void close()}>
            Fermer
          </Button>
          {!readOnly && (
            <Button variant="primary" icon={Save} kbd="⌘S" onClick={() => void save()} loading={spec.saving} disabled={!spec.dirty}>
              Enregistrer
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-4">
        {locked && (
          <Callout tone="warning" icon={Lock} action={lockAction}>
            <div className="space-y-2">
              <div>{lockMessage ?? "Ce fichier est verrouillé en lecture seule."}</div>
              {unlockable && <Switch size="sm" checked={unlocked} onChange={setUnlocked} label="Autoriser la modification" />}
            </div>
          </Callout>
        )}
        {children}
        {spec.file.isError ? (
          <Callout tone="danger" icon={CircleAlert} title="Impossible de lire le fichier">
            {errorMessage(spec.file.error)}
          </Callout>
        ) : !spec.ready ? (
          <LoadingBlock />
        ) : (
          <YamlEditor
            value={spec.content}
            onChange={readOnly ? undefined : spec.setContent}
            onSave={() => void save()}
            readOnly={readOnly}
            errors={spec.errors}
            valid={spec.valid}
            minHeight="420px"
            maxHeight="calc(100vh - 290px)"
          />
        )}
      </div>
    </Drawer>
  );
}
