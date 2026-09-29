import clsx from "clsx";
import { useId, type InputHTMLAttributes, type ReactNode, type Ref, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { ChevronDown, type LucideIcon } from "lucide-react";

const CONTROL =
  "no-drag w-full rounded-[10px] border bg-surface text-fg placeholder:text-faint shadow-sm outline-none transition-[border-color,box-shadow] " +
  "focus:border-accent/60 focus:ring-3 focus:ring-accent/15 disabled:cursor-not-allowed disabled:opacity-60";

export interface FieldProps {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  htmlFor?: string;
  /** Right-aligned element in the label row (e.g. a link). */
  aside?: ReactNode;
  className?: string;
  children: ReactNode;
}

/** Label + control + hint/error layout used by every form control. */
export function Field({ label, hint, error, htmlFor, aside, className, children }: FieldProps) {
  return (
    <div className={clsx("min-w-0", className)}>
      {(label || aside) && (
        <div className="mb-1.5 flex items-center justify-between gap-2">
          {label && (
            <label htmlFor={htmlFor} className="text-[13px] font-medium text-fg">
              {label}
            </label>
          )}
          {aside}
        </div>
      )}
      {children}
      {error ? (
        <p className="mt-1.5 text-[12px] text-danger">{error}</p>
      ) : hint ? (
        <p className="mt-1.5 text-[12px] text-muted">{hint}</p>
      ) : null}
    </div>
  );
}

export interface InputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size"> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  icon?: LucideIcon;
  /** Text or element displayed inside the control on the right (unit…). */
  suffix?: ReactNode;
  size?: "sm" | "md";
  mono?: boolean;
  inputClassName?: string;
  ref?: Ref<HTMLInputElement>;
}

/** Text input with optional label, icon, suffix, hint and error. */
export function Input({
  label,
  hint,
  error,
  icon: Icon,
  suffix,
  size = "md",
  mono,
  className,
  inputClassName,
  id,
  ref,
  ...rest
}: InputProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={inputId} className={className}>
      <div className="relative">
        {Icon && (
          <Icon
            size={15}
            strokeWidth={1.75}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-faint"
            aria-hidden
          />
        )}
        <input
          ref={ref}
          id={inputId}
          className={clsx(
            CONTROL,
            error ? "border-danger/60" : "border-border",
            size === "sm" ? "h-8 px-2.5 text-[13px]" : "h-9 px-3 text-[14px]",
            Icon && (size === "sm" ? "pl-8" : "pl-9"),
            suffix != null && "pr-10",
            mono && "font-mono text-[13px]",
            inputClassName,
          )}
          aria-invalid={!!error || undefined}
          {...rest}
        />
        {suffix != null && (
          <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[12.5px] text-muted">{suffix}</span>
        )}
      </div>
    </Field>
  );
}

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  mono?: boolean;
  ref?: Ref<HTMLTextAreaElement>;
}

/** Multi-line text input. */
export function Textarea({ label, hint, error, mono, className, id, rows = 4, ref, ...rest }: TextareaProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={inputId} className={className}>
      <textarea
        ref={ref}
        id={inputId}
        rows={rows}
        className={clsx(
          CONTROL,
          "resize-y px-3 py-2 text-[14px] leading-relaxed",
          error ? "border-danger/60" : "border-border",
          mono && "font-mono text-[13px]",
        )}
        {...rest}
      />
    </Field>
  );
}

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface SelectProps extends Omit<SelectHTMLAttributes<HTMLSelectElement>, "onChange" | "size" | "value"> {
  label?: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  /** Adds a first empty option with this text. */
  placeholder?: string;
  size?: "sm" | "md";
}

/** Native select (keyboard friendly) with the design-system look. */
export function Select({
  label,
  hint,
  error,
  options,
  value,
  onChange,
  placeholder,
  size = "md",
  className,
  id,
  ...rest
}: SelectProps) {
  const autoId = useId();
  const selectId = id ?? autoId;
  return (
    <Field label={label} hint={hint} error={error} htmlFor={selectId} className={className}>
      <div className="relative">
        <select
          id={selectId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={clsx(
            CONTROL,
            "appearance-none pr-8",
            error ? "border-danger/60" : "border-border",
            size === "sm" ? "h-8 pl-2.5 text-[13px]" : "h-9 pl-3 text-[14px]",
            !value && placeholder && "text-faint",
          )}
          {...rest}
        >
          {placeholder !== undefined && <option value="">{placeholder}</option>}
          {options.map((o) => (
            <option key={o.value} value={o.value} disabled={o.disabled}>
              {o.label}
            </option>
          ))}
        </select>
        <ChevronDown
          size={15}
          strokeWidth={1.75}
          className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-muted"
          aria-hidden
        />
      </div>
    </Field>
  );
}

export interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  size?: "sm" | "md";
  className?: string;
}

/** Toggle switch with optional label/description. */
export function Switch({ checked, onChange, label, description, disabled, size = "md", className }: SwitchProps) {
  const id = useId();
  const track = size === "sm" ? "h-4 w-7" : "h-5 w-9";
  const knob = size === "sm" ? "h-3 w-3" : "h-4 w-4";
  const shift = size === "sm" ? "translate-x-3" : "translate-x-4";
  return (
    <div className={clsx("flex items-start gap-3", className)}>
      <button
        id={id}
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={clsx(
          "no-drag relative mt-0.5 inline-flex shrink-0 items-center rounded-full p-0.5 transition-colors",
          "disabled:cursor-not-allowed disabled:opacity-50",
          track,
          checked ? "bg-accent" : "bg-border-strong",
        )}
      >
        <span className={clsx("rounded-full bg-white shadow-sm transition-transform", knob, checked && shift)} />
      </button>
      {(label || description) && (
        <label htmlFor={id} className="min-w-0 cursor-default">
          {label && <div className="text-[13.5px] font-medium text-fg">{label}</div>}
          {description && <div className="text-[12.5px] text-muted">{description}</div>}
        </label>
      )}
    </div>
  );
}
