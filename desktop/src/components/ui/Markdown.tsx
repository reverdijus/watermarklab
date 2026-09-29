import clsx from "clsx";
import { Fragment, useMemo, type ReactNode } from "react";

/**
 * Dependency-free Markdown renderer (safe: builds React elements, never HTML).
 * Supports headings, paragraphs, bold, italic, strikethrough, inline code,
 * fenced code blocks, links / autolinks, ordered / unordered / task lists
 * (nested by indentation), blockquotes, horizontal rules and pipe tables.
 */

const INLINE_RE =
  /(`+)([\s\S]*?[^`])\1(?!`)|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|~~([\s\S]+?)~~|\*([^*\s][^*]*?)\*|(?<![\w\\])_([^_\s][^_]*?)_(?![\w])|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function safeHref(href: string): string | null {
  const h = href.trim();
  if (/^(https?:|mailto:)/i.test(h)) return h;
  if (h.startsWith("#")) return h;
  return null;
}

function renderLink(href: string, children: ReactNode, key: string): ReactNode {
  const safe = safeHref(href);
  if (!safe) return <Fragment key={key}>{children}</Fragment>;
  const external = !safe.startsWith("#");
  return (
    <a
      key={key}
      href={safe}
      target={external ? "_blank" : undefined}
      rel={external ? "noreferrer" : undefined}
      className="text-accent underline decoration-accent/40 underline-offset-2 hover:decoration-accent"
    >
      {children}
    </a>
  );
}

/** Render inline Markdown into React nodes. */
export function renderInline(text: string, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  INLINE_RE.lastIndex = 0;
  const re = new RegExp(INLINE_RE.source, "g");
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const key = `${keyPrefix}-${n++}`;
    if (m[1]) {
      out.push(
        <code key={key} className="rounded-[5px] bg-surface-2 px-1 py-px font-mono text-[0.88em] text-fg">
          {m[2].trim()}
        </code>,
      );
    } else if (m[3] !== undefined) {
      out.push(renderLink(m[4], renderInline(m[3], key), key));
    } else if (m[5] !== undefined || m[6] !== undefined) {
      out.push(
        <strong key={key} className="font-semibold">
          {renderInline(m[5] ?? m[6], key)}
        </strong>,
      );
    } else if (m[7] !== undefined) {
      out.push(<del key={key}>{renderInline(m[7], key)}</del>);
    } else if (m[8] !== undefined || m[9] !== undefined) {
      out.push(<em key={key}>{renderInline(m[8] ?? m[9], key)}</em>);
    } else if (m[10] !== undefined) {
      out.push(renderLink(m[10], m[10], key));
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function inlineWithBreaks(lines: string[], key: string): ReactNode[] {
  const out: ReactNode[] = [];
  lines.forEach((line, i) => {
    if (i > 0) out.push(<br key={`${key}-br${i}`} />);
    out.push(...renderInline(line.replace(/\s+$/, ""), `${key}-${i}`));
  });
  return out;
}

const RE_FENCE = /^\s*(```|~~~)\s*([\w+-]*)\s*$/;
const RE_HEADING = /^(#{1,6})\s+(.*?)\s*#*\s*$/;
const RE_HR = /^\s*([-*_])(\s*\1){2,}\s*$/;
const RE_QUOTE = /^\s*>\s?/;
const RE_UL = /^(\s*)[-*+]\s+(.*)$/;
const RE_OL = /^(\s*)(\d+)[.)]\s+(.*)$/;
const RE_TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function isBlockStart(line: string): boolean {
  return RE_FENCE.test(line) || RE_HEADING.test(line) || RE_HR.test(line) || RE_QUOTE.test(line) || RE_UL.test(line) || RE_OL.test(line);
}

function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

const HEADING_CLASSES = [
  "font-serif text-[22px] leading-tight mt-6 mb-2",
  "font-serif text-[19px] leading-tight mt-5 mb-2",
  "text-[15.5px] font-semibold mt-4 mb-1.5",
  "text-[14.5px] font-semibold mt-3 mb-1",
  "text-[14px] font-semibold mt-3 mb-1",
  "text-[13.5px] font-semibold text-muted mt-3 mb-1",
];

function parseList(lines: string[], start: number, ordered: boolean, key: string): { node: ReactNode; next: number } {
  const re = ordered ? RE_OL : RE_UL;
  const first = re.exec(lines[start])!;
  const baseIndent = first[1].length;
  const items: string[][] = [];
  let i = start;
  while (i < lines.length) {
    const line = lines[i];
    const m = re.exec(line);
    if (m && m[1].length === baseIndent) {
      items.push([ordered ? m[3] : m[2]]);
      i++;
      continue;
    }
    if (line.trim() === "") {
      // A blank line ends the list unless the next line continues it (indented or same marker).
      const nextLine = lines[i + 1];
      if (nextLine !== undefined && nextLine.trim() !== "") {
        const nm = re.exec(nextLine);
        const nextIndent = /^(\s*)/.exec(nextLine)![1].length;
        if ((nm && nm[1].length === baseIndent) || nextIndent > baseIndent) {
          items[items.length - 1].push("");
          i++;
          continue;
        }
      }
      break;
    }
    const indent = /^(\s*)/.exec(line)![1].length;
    if (indent > baseIndent) {
      items[items.length - 1].push(line.slice(Math.min(indent, baseIndent + (ordered ? 3 : 2))));
      i++;
      continue;
    }
    if (isBlockStart(line)) break;
    items[items.length - 1].push(line.trim()); // lazy continuation
    i++;
  }
  const startNum = ordered ? Number(first[2]) : 1;
  const children = items.map((itemLines, idx) => {
    const text = itemLines[0];
    const task = /^\[( |x|X)\]\s+(.*)$/.exec(text);
    const rest = itemLines.slice(1);
    const head = task ? task[2] : text;
    return (
      <li key={idx} className={clsx("pl-1", task && "list-none -ml-5 flex items-start gap-2")}>
        {task && (
          <input type="checkbox" checked={task[1] !== " "} readOnly className="mt-1 accent-[var(--accent)]" aria-hidden />
        )}
        <div className="min-w-0">
          {renderInline(head, `${key}-li${idx}`)}
          {rest.some((l) => l.trim()) && <div className="mt-1">{parseBlocks(rest, `${key}-li${idx}`)}</div>}
        </div>
      </li>
    );
  });
  const node = ordered ? (
    <ol key={key} start={startNum} className="my-2 list-decimal space-y-1 pl-6 marker:text-muted">
      {children}
    </ol>
  ) : (
    <ul key={key} className="my-2 list-disc space-y-1 pl-6 marker:text-faint">
      {children}
    </ul>
  );
  return { node, next: i };
}

function parseBlocks(lines: string[], keyPrefix: string): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  let n = 0;
  while (i < lines.length) {
    const line = lines[i];
    const key = `${keyPrefix}-b${n++}`;
    if (line.trim() === "") {
      i++;
      continue;
    }
    const fence = RE_FENCE.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${fence[1]}\\s*$`).test(lines[i])) body.push(lines[i++]);
      i++; // closing fence
      out.push(
        <pre
          key={key}
          className="selectable my-3 overflow-x-auto rounded-xl border border-border bg-surface-2 px-4 py-3 font-mono text-[12.5px] leading-relaxed"
        >
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }
    const heading = RE_HEADING.exec(line);
    if (heading) {
      const level = heading[1].length;
      const Tag = `h${level}` as "h1";
      out.push(
        <Tag key={key} className={clsx("text-fg first:mt-0", HEADING_CLASSES[level - 1])}>
          {renderInline(heading[2], key)}
        </Tag>,
      );
      i++;
      continue;
    }
    if (RE_HR.test(line)) {
      out.push(<hr key={key} className="my-5 border-border" />);
      i++;
      continue;
    }
    if (RE_QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && RE_QUOTE.test(lines[i])) body.push(lines[i++].replace(RE_QUOTE, ""));
      out.push(
        <blockquote key={key} className="my-3 border-l-2 border-accent/50 pl-4 text-muted">
          {parseBlocks(body, key)}
        </blockquote>,
      );
      continue;
    }
    if (RE_UL.test(line) || RE_OL.test(line)) {
      const { node, next } = parseList(lines, i, !RE_UL.test(line), key);
      out.push(node);
      i = next;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && RE_TABLE_SEP.test(lines[i + 1])) {
      const header = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map((c) => (c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : "left"));
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") rows.push(splitRow(lines[i++]));
      out.push(
        <div key={key} className="my-3 overflow-x-auto rounded-xl border border-border">
          <table className="w-full border-collapse text-[13px]">
            <thead className="bg-surface-2">
              <tr>
                {header.map((h, c) => (
                  <th key={c} className="px-3 py-1.5 font-medium" style={{ textAlign: aligns[c] as "left" }}>
                    {renderInline(h, `${key}-h${c}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri} className="border-t border-border">
                  {header.map((_, c) => (
                    <td key={c} className="px-3 py-1.5" style={{ textAlign: aligns[c] as "left" }}>
                      {renderInline(r[c] ?? "", `${key}-${ri}-${c}`)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>,
      );
      continue;
    }
    // Paragraph
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && (para.length === 0 || !isBlockStart(lines[i]))) {
      if (para.length > 0 && lines[i].includes("|") && RE_TABLE_SEP.test(lines[i + 1] ?? "")) break;
      para.push(lines[i++]);
    }
    out.push(
      <p key={key} className="my-2 first:mt-0 last:mb-0">
        {inlineWithBreaks(para, key)}
      </p>,
    );
  }
  return out;
}

export interface MarkdownProps {
  source: string;
  className?: string;
  /** Rendered when the source is empty. */
  empty?: ReactNode;
}

/** Render a Markdown string with the design-system typography. */
export function Markdown({ source, className, empty }: MarkdownProps) {
  const nodes = useMemo(() => parseBlocks(source.replace(/\r\n?/g, "\n").split("\n"), "md"), [source]);
  if (!source.trim()) return <div className={clsx("text-[14px] text-muted", className)}>{empty ?? "—"}</div>;
  return <div className={clsx("selectable text-[14px] leading-[1.65] text-fg", className)}>{nodes}</div>;
}
