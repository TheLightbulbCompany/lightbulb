import * as React from "react"
import { Slot as SlotPrimitive } from "radix-ui"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/* One button dialect. `buttonVariants` wears the Editorial pill (999px radius,
 * semibold label, midnight fill / cream label) so the ~50 shadcn call sites and the
 * hand-written `.editorial-btn` in globals.css render as the same control.
 * Surfaces are named through the shadcn token aliases rather than the brand
 * primitives directly — the aliases resolve to exactly those primitives on
 * cream AND flip correctly under `.dark`, which /admin applies.
 * Focus comes from the one global `:focus-visible` outline; only the
 * aria-invalid (validation, not focus) treatment is carried here.
 * Icons default to 16px unless sized: lucide writes `size={12|20}` as width/
 * height attributes with no `size-*` class, so those two scale steps are
 * excluded from the default rather than being forced back to 16. */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-full border border-transparent text-sm font-semibold transition-[color,background-color,border-color,box-shadow,transform,scale] duration-(--dur-fast) ease-out active:scale-[0.97] disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg:not([class*='size-']):not([width='12']):not([width='20'])]:size-4 shrink-0 [&_svg]:shrink-0 aria-invalid:ring-destructive/20 aria-invalid:border-destructive",
  {
    variants: {
      variant: {
        // Filled variants get a real disabled SURFACE, not a blanket alpha: at
        // 50% a midnight fill composites to a washed sage and drags its cream
        // label down with it, reading as a ghostly blob rather than a state.
        default:
          "bg-primary text-primary-foreground hover:bg-[color-mix(in_srgb,var(--primary)_88%,var(--primary-foreground))] disabled:opacity-100 disabled:bg-muted disabled:text-muted-foreground disabled:border-border",
        destructive:
          "bg-destructive text-destructive-foreground hover:bg-[color-mix(in_srgb,var(--destructive)_88%,var(--destructive-foreground))] disabled:opacity-100 disabled:bg-muted disabled:text-muted-foreground disabled:border-border",
        outline:
          "border-border bg-background hover:bg-accent hover:text-accent-foreground",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-[color-mix(in_srgb,var(--secondary)_88%,var(--secondary-foreground))]",
        ghost:
          "hover:bg-accent hover:text-accent-foreground",
        link: "text-primary underline-offset-4 hover:underline",
      },
      size: {
        default: "h-9 px-4 has-[>svg]:px-3",
        sm: "h-8 gap-1.5 px-3 has-[>svg]:px-2.5",
        lg: "h-10 px-5 has-[>svg]:px-4",
        // Icon-only controls press a touch deeper — there is less area to read
        // the scale off.
        icon: "size-9 active:scale-95",
        "icon-sm": "size-8 active:scale-95",
        "icon-lg": "size-10 active:scale-95",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
)

function Button({
  className,
  variant = "default",
  size = "default",
  asChild = false,
  ...props
}: React.ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & {
    asChild?: boolean
  }) {
  const Comp = asChild ? SlotPrimitive.Root : "button"

  return (
    <Comp
      data-slot="button"
      data-variant={variant}
      data-size={size}
      className={cn(buttonVariants({ variant, size, className }))}
      {...props}
    />
  )
}

export { Button, buttonVariants }
