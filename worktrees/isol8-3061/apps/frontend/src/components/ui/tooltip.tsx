"use client"

import * as React from "react"
import { Tooltip as TooltipPrimitive } from "radix-ui"

import { cn } from "@/lib/utils"

/**
 * The product's tooltip. Replaces native `title=` on icon-only controls, which
 * has no styling, no keyboard story and a ~1s browser delay nobody can tune.
 *
 * `delayDuration` waits before the FIRST tooltip so a cursor crossing a toolbar
 * does not strobe; `skipDelayDuration` then opens neighbours instantly for a
 * moment after one closes, which is what makes a toolbar feel fast.
 * Mount `TooltipProvider` once at the app root (app/layout.tsx) so that grace
 * period is shared across every toolbar on the page.
 */
const DEFAULT_DELAY = 400
const DEFAULT_SKIP_DELAY = 200

/** Radix throws when a Tooltip has no Provider above it, and it exports no way
 *  to ask. This is that question: `TooltipProvider` answers true, so `Tooltip`
 *  can supply its own only when it is genuinely on its own (an isolated render
 *  in a unit test, a detached subtree) instead of shadowing the app-level
 *  provider — which would scope the skip-delay grace to one tooltip and undo
 *  the point of having it. */
const HasTooltipProvider = React.createContext(false)

function TooltipProvider({
  delayDuration = DEFAULT_DELAY,
  skipDelayDuration = DEFAULT_SKIP_DELAY,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Provider>) {
  return (
    <HasTooltipProvider.Provider value={true}>
      <TooltipPrimitive.Provider
        data-slot="tooltip-provider"
        delayDuration={delayDuration}
        skipDelayDuration={skipDelayDuration}
        {...props}
      />
    </HasTooltipProvider.Provider>
  )
}

function Tooltip(props: React.ComponentProps<typeof TooltipPrimitive.Root>) {
  const provided = React.useContext(HasTooltipProvider)
  const root = <TooltipPrimitive.Root data-slot="tooltip" {...props} />
  return provided ? root : <TooltipProvider>{root}</TooltipProvider>
}

const TooltipTrigger = TooltipPrimitive.Trigger

function TooltipContent({
  className,
  sideOffset = 6,
  children,
  ...props
}: React.ComponentProps<typeof TooltipPrimitive.Content>) {
  return (
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        data-slot="tooltip-content"
        sideOffset={sideOffset}
        className={cn(
          "z-(--z-overlay) w-fit rounded-md bg-primary px-2 py-1 text-caption text-balance text-primary-foreground shadow-md",
          "origin-(--radix-tooltip-content-transform-origin) ease-out duration-(--dur-fast)",
          "data-[state=delayed-open]:animate-in data-[state=delayed-open]:fade-in-0 data-[state=delayed-open]:zoom-in-95",
          "data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95",
          className
        )}
        {...props}
      >
        {children}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  )
}

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
