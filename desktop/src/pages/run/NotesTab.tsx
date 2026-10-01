import { NotesEditor } from "../../components/NotesEditor";
import { useSetRunNotes } from "../../lib/queries";
import type { RunDetail } from "../../lib/types";

export function NotesTab({ run }: { run: RunDetail }) {
  const save = useSetRunNotes(run.run_id);
  return (
    <NotesEditor
      draftKey={`run-notes:${run.run_id}`}
      notes={run.notes}
      save={(notes) => save.mutateAsync(notes)}
      saving={save.isPending}
      description={
        <>
          Hypothèse, observations et conclusion du run — enregistrées dans <span className="font-mono">notes.md</span>.
        </>
      }
    />
  );
}
