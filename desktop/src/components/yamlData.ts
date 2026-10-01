/**
 * Read-only YAML → JS conversion for spec previews (search space, summary panels…),
 * built on the Lezer YAML parser already shipped with the editor. It covers what the
 * workspace specs use (block/flow collections, plain/quoted/block scalars, anchors) and
 * resolves plain scalars like PyYAML (null, booleans, ints, floats). Never throws:
 * syntax errors simply yield partial data (the backend remains the validator).
 */
import { yamlLanguage } from "@codemirror/lang-yaml";

type Tree = ReturnType<typeof yamlLanguage.parser.parse>;
type Node = Tree["topNode"];

const VALUE_NODES = new Set([
  "Literal",
  "QuotedLiteral",
  "BlockLiteral",
  "FlowMapping",
  "FlowSequence",
  "BlockMapping",
  "BlockSequence",
  "Tagged",
  "Anchored",
  "Alias",
]);

const NULLS = new Set(["", "~", "null", "Null", "NULL"]);
const TRUES = new Set(["true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"]);
const FALSES = new Set(["false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"]);
const INT_RE = /^[-+]?(0|[1-9][0-9_]*)$/;
const FLOAT_RE = /^[-+]?(?:[0-9][0-9_]*)?(?:\.[0-9_]*)?(?:[eE][-+]?[0-9]+)?$/;

/** Resolve a plain (unquoted) scalar the way PyYAML's safe loader does (numbers are lenient). */
export function resolvePlainScalar(raw: string): unknown {
  const s = raw.trim();
  if (NULLS.has(s)) return null;
  if (TRUES.has(s)) return true;
  if (FALSES.has(s)) return false;
  if (INT_RE.test(s)) return Number(s.replace(/_/g, ""));
  if (/^0x[0-9a-fA-F_]+$/.test(s)) return parseInt(s.slice(2).replace(/_/g, ""), 16);
  if (/^0o[0-7_]+$/.test(s)) return parseInt(s.slice(2).replace(/_/g, ""), 8);
  if (/[0-9]/.test(s) && FLOAT_RE.test(s)) {
    const n = Number(s.replace(/_/g, ""));
    if (Number.isFinite(n)) return n;
  }
  if (/^[-+]?\.(inf|Inf|INF)$/.test(s)) return s.startsWith("-") ? -Infinity : Infinity;
  if (/^\.(nan|NaN|NAN)$/.test(s)) return NaN;
  return s;
}

/** Fold a multi-line flow scalar: line breaks become spaces, empty lines become "\n". */
function foldLines(text: string): string {
  const lines = text.split("\n").map((l) => l.trim());
  let out = "";
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (i === 0) {
      out = line;
      continue;
    }
    if (line === "") out += "\n";
    else out += out.endsWith("\n") || out === "" ? line : ` ${line}`;
  }
  return out;
}

function unquote(raw: string): string {
  const quote = raw[0];
  const body = raw.length >= 2 && raw.endsWith(quote) ? raw.slice(1, -1) : raw.slice(1);
  if (quote === "'") return foldLines(body).replace(/''/g, "'");
  const folded = foldLines(body);
  try {
    return JSON.parse(`"${folded.replace(/\n/g, "\\n").replace(/\t/g, "\\t")}"`) as string;
  } catch {
    return folded.replace(/\\"/g, '"').replace(/\\n/g, "\n").replace(/\\\\/g, "\\");
  }
}

function blockScalar(header: string, content: string): string {
  const literal = header.startsWith("|");
  const chomp = header.includes("-") ? "strip" : header.includes("+") ? "keep" : "clip";
  const lines = content.split("\n");
  const explicit = /\d/.exec(header);
  const indents = lines.filter((l) => l.trim() !== "").map((l) => l.length - l.trimStart().length);
  const indent = explicit ? Number(explicit[0]) : indents.length ? Math.min(...indents) : 0;
  const stripped = lines.map((l) => l.slice(Math.min(indent, l.length - l.trimStart().length)));
  let body: string;
  if (literal) {
    body = stripped.join("\n");
  } else {
    body = "";
    stripped.forEach((line, i) => {
      if (i === 0) body = line;
      else if (line === "" || line.startsWith(" ") || body.endsWith("\n") || body === "") body += `\n${line}`;
      else body += ` ${line}`;
    });
  }
  const trimmed = body.replace(/\n+$/, "");
  if (chomp === "strip") return trimmed;
  if (chomp === "keep") return body.endsWith("\n") ? body : `${body}\n`;
  return trimmed ? `${trimmed}\n` : "";
}

class Converter {
  private readonly anchors = new Map<string, unknown>();
  private readonly text: string;

  constructor(text: string) {
    this.text = text;
  }

  private src(node: Node): string {
    return this.text.slice(node.from, node.to);
  }

  private children(node: Node): Node[] {
    const out: Node[] = [];
    for (let c = node.firstChild; c; c = c.nextSibling) out.push(c);
    return out;
  }

  private valueChild(node: Node): Node | null {
    return this.children(node).find((c) => VALUE_NODES.has(c.name)) ?? null;
  }

  value(node: Node | null): unknown {
    if (!node) return null;
    switch (node.name) {
      case "Stream":
      case "Document":
        return this.value(this.children(node).find((c) => c.name === "Document" || VALUE_NODES.has(c.name)) ?? null);
      case "BlockMapping":
      case "FlowMapping": {
        const out: Record<string, unknown> = {};
        for (const pair of this.children(node)) {
          if (pair.name !== "Pair") continue;
          const keyNode = pair.getChild("Key");
          const key = keyNode ? this.value(this.valueChild(keyNode)) : null;
          const valueNode = this.children(pair).find((c) => c.name !== "Key" && VALUE_NODES.has(c.name)) ?? null;
          out[String(key)] = this.value(valueNode);
        }
        return out;
      }
      case "BlockSequence":
      case "FlowSequence":
        return this.children(node)
          .filter((c) => c.name === "Item")
          .map((item) => this.value(this.valueChild(item)));
      case "Literal":
        return resolvePlainScalar(foldLines(this.src(node)));
      case "QuotedLiteral":
        return unquote(this.src(node));
      case "BlockLiteral": {
        const header = node.getChild("BlockLiteralHeader");
        const content = node.getChild("BlockLiteralContent");
        return blockScalar(header ? this.src(header) : "|", content ? this.src(content) : "");
      }
      case "Anchored": {
        const anchor = node.getChild("Anchor");
        const value = this.value(this.valueChild(node));
        if (anchor) this.anchors.set(this.src(anchor).slice(1), value);
        return value;
      }
      case "Tagged":
        return this.value(this.valueChild(node));
      case "Alias":
        return this.anchors.get(this.src(node).slice(1)) ?? null;
      default:
        return null;
    }
  }
}

/** Parse the first YAML document of `text` into plain JS values (null when empty). */
export function parseYaml(text: string): unknown {
  if (!text.trim()) return null;
  try {
    const tree = yamlLanguage.parser.parse(text);
    return new Converter(text).value(tree.topNode);
  } catch {
    return null;
  }
}

/** Parse YAML and return it only when it is a mapping. */
export function parseYamlObject(text: string): Record<string, unknown> | null {
  const v = parseYaml(text);
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

/**
 * Replace the value of a top-level `key: value` line, keeping its trailing comment;
 * the line is prepended when the key is missing. Used to derive new spec files from a copy.
 */
export function setTopLevelScalar(text: string, key: string, value: string): string {
  const escaped = key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(`^${escaped}:[ \\t]*([^#\\n]*?)([ \\t]+#[^\\n]*)?$`, "m");
  if (re.test(text)) return text.replace(re, (_m, _v, comment?: string) => `${key}: ${value}${comment ?? ""}`);
  return `${key}: ${value}\n${text}`;
}
