import clsx from "clsx";
import { useMemo, useRef } from "react";
import CodeMirror, { EditorView, keymap, Prec, type Extension } from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { tags } from "@lezer/highlight";
import { CircleAlert, CircleCheck } from "lucide-react";
import type { ValidationError } from "../lib/types";

/**
 * Editor theme written with CSS variables so it follows light/dark tokens
 * without re-creating the editor.
 */
const editorTheme = EditorView.theme({
  "&": {
    color: "var(--text)",
    backgroundColor: "var(--surface)",
    fontSize: "12.5px",
    height: "100%",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": {
    fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace',
    lineHeight: "1.65",
  },
  ".cm-content": { caretColor: "var(--accent)", padding: "10px 0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--accent)", borderLeftWidth: "2px" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection":
    { backgroundColor: "color-mix(in srgb, var(--accent) 22%, transparent) !important" },
  ".cm-gutters": {
    backgroundColor: "var(--surface)",
    color: "var(--faint)",
    border: "none",
    borderRight: "1px solid var(--border)",
  },
  ".cm-activeLineGutter": { backgroundColor: "transparent", color: "var(--text)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--text) 3.5%, transparent)" },
  ".cm-lineNumbers .cm-gutterElement": { padding: "0 10px 0 14px" },
  ".cm-foldGutter .cm-gutterElement": { color: "var(--faint)" },
  ".cm-matchingBracket, .cm-nonmatchingBracket": {
    backgroundColor: "color-mix(in srgb, var(--accent) 16%, transparent)",
    outline: "none",
  },
  ".cm-searchMatch": { backgroundColor: "color-mix(in srgb, var(--warning) 25%, transparent)" },
  ".cm-searchMatch.cm-searchMatch-selected": { backgroundColor: "color-mix(in srgb, var(--warning) 45%, transparent)" },
  ".cm-panels": { backgroundColor: "var(--surface-2)", color: "var(--text)", borderColor: "var(--border)" },
  ".cm-panels input, .cm-panels button": { fontSize: "12px" },
  ".cm-tooltip": {
    backgroundColor: "var(--surface)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    color: "var(--text)",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": { backgroundColor: "var(--accent-soft)", color: "var(--text)" },
  ".cm-placeholder": { color: "var(--faint)" },
});

const highlight = HighlightStyle.define([
  { tag: [tags.propertyName, tags.definition(tags.propertyName)], color: "var(--accent)" },
  { tag: [tags.string, tags.special(tags.string)], color: "var(--success)" },
  { tag: [tags.number, tags.bool, tags.null, tags.atom], color: "var(--info)" },
  { tag: [tags.comment, tags.lineComment, tags.blockComment], color: "var(--muted)", fontStyle: "italic" },
  { tag: [tags.keyword, tags.operator, tags.punctuation, tags.separator], color: "var(--muted)" },
  { tag: [tags.meta, tags.labelName, tags.typeName], color: "var(--warning)" },
  { tag: tags.content, color: "var(--text)" },
]);

const NO_EXTENSIONS: Extension[] = [];

export interface YamlEditorProps {
  value: string;
  onChange?: (value: string) => void;
  /** Cmd+S / Ctrl+S handler. */
  onSave?: () => void;
  readOnly?: boolean;
  /** Validation errors listed under the editor. */
  errors?: ValidationError[];
  /** Validation state: true = valid, false = invalid, undefined = unknown (no footer). */
  valid?: boolean;
  /** Show the validation footer (default: when `valid` or `errors` are provided). */
  showValidation?: boolean;
  height?: string;
  minHeight?: string;
  maxHeight?: string;
  placeholder?: string;
  className?: string;
  autoFocus?: boolean;
  /** Extra CodeMirror extensions. */
  extensions?: Extension[];
}

/** YAML editor (CodeMirror 6) with themed tokens, Cmd+S and a validation panel. */
export function YamlEditor({
  value,
  onChange,
  onSave,
  readOnly,
  errors,
  valid,
  showValidation,
  height,
  minHeight = "240px",
  maxHeight,
  placeholder,
  className,
  autoFocus,
  extensions: extra = NO_EXTENSIONS,
}: YamlEditorProps) {
  const saveRef = useRef(onSave);
  saveRef.current = onSave;

  const extensions = useMemo<Extension[]>(
    () => [
      yaml(),
      editorTheme,
      syntaxHighlighting(highlight),
      EditorView.lineWrapping,
      Prec.highest(
        keymap.of([
          {
            key: "Mod-s",
            preventDefault: true,
            run: () => {
              saveRef.current?.();
              return true;
            },
          },
        ]),
      ),
      ...extra,
    ],
    [extra],
  );

  const errs = errors ?? [];
  const footer = showValidation ?? (valid !== undefined || errors !== undefined);
  const isValid = valid ?? errs.length === 0;

  return (
    <div className={clsx("no-drag overflow-hidden rounded-xl border border-border bg-surface shadow-sm", className)}>
      <CodeMirror
        value={value}
        onChange={onChange}
        readOnly={readOnly}
        editable={!readOnly}
        theme="none"
        extensions={extensions}
        height={height}
        minHeight={minHeight}
        maxHeight={maxHeight}
        placeholder={placeholder}
        autoFocus={autoFocus}
        basicSetup={{
          lineNumbers: true,
          foldGutter: true,
          highlightActiveLine: !readOnly,
          highlightActiveLineGutter: !readOnly,
          autocompletion: false,
          searchKeymap: true,
          bracketMatching: true,
          indentOnInput: true,
          tabSize: 2,
        }}
        indentWithTab
      />
      {footer && (
        <div className="border-t border-border bg-surface-2/60 px-4 py-2.5">
          {isValid && errs.length === 0 ? (
            <div className="flex items-center gap-2 text-[12.5px] text-success">
              <CircleCheck size={14} strokeWidth={1.75} aria-hidden />
              Spécification valide
            </div>
          ) : (
            <div>
              <div className="flex items-center gap-2 text-[12.5px] font-medium text-danger">
                <CircleAlert size={14} strokeWidth={1.75} aria-hidden />
                {errs.length > 0 ? `${errs.length} erreur${errs.length > 1 ? "s" : ""} de validation` : "Spécification invalide"}
              </div>
              {errs.length > 0 && (
                <ul className="mt-1.5 max-h-40 space-y-1 overflow-y-auto">
                  {errs.map((e, i) => (
                    <li key={i} className="selectable flex gap-2 text-[12.5px] leading-snug">
                      {e.loc && <code className="shrink-0 font-mono text-[11.5px] text-muted">{e.loc}</code>}
                      <span className="text-fg">{e.msg}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
