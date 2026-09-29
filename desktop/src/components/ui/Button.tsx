import clsx from "clsx";
import type { ButtonHTMLAttributes, ReactNode, Ref } from "react";
import { Link } from "react-router";
import type { LucideIcon } from "lucide-react";
import { Spinner } from "./Spinner";
import { Tooltip } from "./Tooltip";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md";

const VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-fg hover:bg-accent-hover shadow-sm border border-transparent",
  secondary: "bg-surface text-fg border border-border hover:bg-surface-2 hover:border-border-strong shadow-sm",
  ghost: "bg-transparent text-fg border border-transparent hover:bg-hover",
  danger: "bg-danger/8 text-danger border border-danger/25 hover:bg-danger/14",
};

const SIZES: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-[13px] gap-1.5 rounded-lg",
  md: "h-9 px-3.5 text-[14px] gap-2 rounded-[10px]",
};

const ICON_SIZES: Record<ButtonSize, number> = { sm: 14, md: 16 };

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Leading icon (lucide). */
  icon?: LucideIcon;
  /** Trailing icon (lucide). */
  iconRight?: LucideIcon;
  /** Shows a spinner and disables the button. */
  loading?: boolean;
  /** Renders a router link with the button look. */
  to?: string;
  /** Keyboard shortcut hint, e.g. "⌘N". */
  kbd?: string;
  fullWidth?: boolean;
  ref?: Ref<HTMLButtonElement>;
}

/** Button with primary/secondary/ghost/danger variants and sm/md sizes. */
export function Button({
  variant = "secondary",
  size = "md",
  icon: Icon,
  iconRight: IconRight,
  loading = false,
  to,
  kbd,
  fullWidth,
  className,
  children,
  disabled,
  type = "button",
  ref,
  ...rest
}: ButtonProps) {
  const classes = clsx(
    "no-drag inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap font-medium transition-colors duration-100",
    "disabled:pointer-events-none disabled:opacity-50",
    VARIANTS[variant],
    SIZES[size],
    fullWidth && "w-full",
    className,
  );
  const iconSize = ICON_SIZES[size];
  const content: ReactNode = (
    <>
      {loading ? <Spinner size={iconSize} /> : Icon ? <Icon size={iconSize} strokeWidth={1.75} aria-hidden /> : null}
      {children != null && <span className="truncate">{children}</span>}
      {IconRight && !loading && <IconRight size={iconSize} strokeWidth={1.75} aria-hidden />}
      {kbd && (
        <span className={clsx("ml-1 text-[11px] font-normal", variant === "primary" ? "opacity-75" : "text-faint")}>
          {kbd}
        </span>
      )}
    </>
  );
  if (to && !disabled) {
    return (
      <Link to={to} className={classes} title={rest.title} onClick={rest.onClick as never}>
        {content}
      </Link>
    );
  }
  return (
    <button ref={ref} type={type} className={classes} disabled={disabled || loading} {...rest}>
      {content}
    </button>
  );
}

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  icon: LucideIcon;
  /** Accessible label, also used as tooltip. */
  label: string;
  size?: ButtonSize;
  variant?: "ghost" | "secondary";
  active?: boolean;
  tooltip?: boolean;
  tooltipSide?: "top" | "bottom" | "left" | "right";
  ref?: Ref<HTMLButtonElement>;
}

/** Square icon-only button with a tooltip. */
export function IconButton({
  icon: Icon,
  label,
  size = "md",
  variant = "ghost",
  active,
  tooltip = true,
  tooltipSide = "bottom",
  className,
  type = "button",
  ref,
  ...rest
}: IconButtonProps) {
  const button = (
    <button
      ref={ref}
      type={type}
      aria-label={label}
      className={clsx(
        "no-drag inline-flex shrink-0 items-center justify-center rounded-lg text-muted transition-colors duration-100",
        "hover:bg-hover hover:text-fg disabled:pointer-events-none disabled:opacity-40",
        size === "sm" ? "h-7 w-7" : "h-8 w-8",
        variant === "secondary" && "border border-border bg-surface shadow-sm",
        active && "bg-active text-fg",
        className,
      )}
      {...rest}
    >
      <Icon size={size === "sm" ? 15 : 17} strokeWidth={1.75} aria-hidden />
    </button>
  );
  return tooltip ? (
    <Tooltip content={label} side={tooltipSide}>
      {button}
    </Tooltip>
  ) : (
    button
  );
}
