/**
 * Toast after `PUT /api/files`: in hosted mode the response carries the git outcome
 * (§12.3) — "Enregistré · commité abc1234 · poussé", or a warning when the commit was not
 * pushed; outside hosted mode a plain "Enregistré".
 */
import { describeFileSave } from "../lib/hosted";
import type { SaveFileResult } from "../lib/types";
import type { ToastApi } from "./ui";

export function toastFileSaved(
  toast: ToastApi,
  res: Pick<SaveFileResult, "git">,
  title: string,
  detail: string | null = null,
  opts: { forceWarning?: boolean } = {},
): void {
  const notice = describeFileSave(res.git, title, detail);
  if (notice.tone === "warning" || opts.forceWarning) toast.warning(notice.title, notice.description ?? undefined);
  else toast.success(notice.title, notice.description ?? undefined);
}
