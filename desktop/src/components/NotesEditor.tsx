import clsx from "clsx";
import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { Button, Card, Markdown, Segmented, useToast } from "./ui";
import { errorMessage } from "../lib/api";
import { rememberDraft, restoreDraft } from "../lib/drafts";
import { useHotkey } from "../lib/hooks";

type Mode = "edit" | "split" | "preview";

export interface NotesEditorProps {
  /** Draft-store key (`run-notes:<id>`, `study-notes:<id>`): the editor unmounts on every tab change. */
  draftKey: string;
  /** Current server content of notes.md. */
  notes: string;
  /** Persists the text; rejects with an API error. */
  save: (notes: string) => Promise<unknown>;
  saving?: boolean;
  /** Short sentence shown next to the mode switch. */
  description: React.ReactNode;
  placeholder?: string;
}

/**
 * Markdown notes editor (edit / split / preview) with ⌘S, unsaved-draft memory across
 * navigation and server-side change following while there are no local edits.
 */
export function NotesEditor({ draftKey, notes, save, saving = false, description, placeholder }: NotesEditorProps) {
  // An unsaved draft from a previous visit (other tab / page) is restored when notes.md
  // has not changed on the server in the meantime.
  const [draft, setDraft] = useState(() => restoreDraft(draftKey, notes) ?? notes);
  const [base, setBase] = useState(notes);
  const [mode, setMode] = useState<Mode>("split");
  const toast = useToast();
  const dirty = draft !== base;

  // Follow server-side changes while there are no local edits.
  useEffect(() => {
    if (!dirty && notes !== base) {
      setDraft(notes);
      setBase(notes);
    }
  }, [notes, base, dirty]);

  // Remember the unsaved draft across tab / page changes (and guard ⌘R / window close).
  useEffect(() => {
    rememberDraft(draftKey, draft, base);
  }, [draftKey, draft, base]);

  const onSave = async () => {
    if (!dirty) return;
    try {
      await save(draft);
      setBase(draft);
      toast.success("Notes enregistrées");
    } catch (err) {
      toast.error("Notes non enregistrées", errorMessage(err));
    }
  };

  useHotkey("mod+s", () => void onSave());

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Segmented<Mode>
          value={mode}
          onChange={setMode}
          options={[
            { value: "edit", label: "Éditer" },
            { value: "split", label: "Côte à côte" },
            { value: "preview", label: "Aperçu" },
          ]}
        />
        <span className="text-[12.5px] text-muted">{description}</span>
        <div className="ml-auto flex items-center gap-3">
          {dirty && <span className="text-[12.5px] text-warning">Modifications non enregistrées</span>}
          <Button variant="primary" icon={Save} onClick={() => void onSave()} loading={saving} disabled={!dirty} kbd="⌘S">
            Enregistrer
          </Button>
        </div>
      </div>
      <div className={clsx("grid gap-4", mode === "split" ? "grid-cols-2" : "grid-cols-1")}>
        {mode !== "preview" && (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck
            lang="fr"
            placeholder={placeholder ?? "## Conclusion\n\n- …"}
            aria-label="Notes (Markdown)"
            className="no-drag h-[calc(100vh-300px)] min-h-[360px] w-full resize-none rounded-xl border border-border bg-surface px-4 py-3 font-mono text-[13px] leading-relaxed text-fg shadow-sm outline-none focus:border-accent/60 focus:ring-3 focus:ring-accent/15"
          />
        )}
        {mode !== "edit" && (
          <Card className="h-[calc(100vh-300px)] min-h-[360px] overflow-y-auto">
            <Markdown source={draft} empty="Rien à afficher pour l'instant." />
          </Card>
        )}
      </div>
    </div>
  );
}
