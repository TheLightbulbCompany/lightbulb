"use client";

import { Component, ErrorInfo, ReactNode } from "react";

import { isChunkLoadError, reloadForChunkError } from "@/components/DeployRefresh";
import { SupportTicketDialog } from "@/components/support/SupportTicketDialog";
import { captureException } from "@/lib/analytics";

// PostHogInit (a sibling under PostHogProvider) loads PostHog in an effect after this commit, and
// captureException does nothing until it has. ponytail: one fixed delay, not a queue; an error caught
// before PostHog loads within this window is still lost.
export const REPORT_DELAY_MS = 2000;

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  componentStack: string | null;
  reportOpen: boolean;
}

export class ErrorBoundary extends Component<Props, State> {
  // ponytail: one silent retry per boundary (the root one lives for the whole page load). The first render
  // after an OAuth redirect threw once and passed on Try Again (2026-11-23). A second throw shows the screen.
  private retried = false;

  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null, componentStack: null, reportOpen: false };
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    // A lazy chunk from the previous deploy: reload onto the new one instead
    // of showing "Something went wrong".
    if (isChunkLoadError(error)) return reloadForChunkError();
    console.error("Error caught by boundary:", error, errorInfo);
    const route = window.location.pathname;
    setTimeout(() => captureException(error, { route, source: "error_boundary" }), REPORT_DELAY_MS);
    if (!this.retried) {
      this.retried = true;
      return this.handleRetry();
    }
    this.setState({ componentStack: errorInfo.componentStack ?? null });
  }

  private handleRetry = (): void => {
    this.setState({ hasError: false, error: null, componentStack: null, reportOpen: false });
  };

  render(): ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      const { error, componentStack } = this.state;
      const stack = [error?.stack, componentStack && `Component stack:${componentStack}`]
        .filter(Boolean)
        .join("\n\n");

      return (
        <div className="flex flex-col items-center justify-center min-h-screen p-4">
          <div className="max-w-md text-center">
            <h2 className="text-2xl font-bold mb-4">Something went wrong</h2>
            <p className="text-muted-foreground mb-6">
              An unexpected error occurred. Please try refreshing the page.
            </p>
            <div className="space-x-4">
              <button
                onClick={this.handleRetry}
                className="px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90"
              >
                Try Again
              </button>
              <button
                onClick={() => window.location.reload()}
                className="px-4 py-2 border rounded-md hover:bg-muted"
              >
                Refresh Page
              </button>
              <button
                onClick={() => this.setState({ reportOpen: true })}
                className="px-4 py-2 border rounded-md hover:bg-muted"
              >
                Report this error
              </button>
            </div>
            {process.env.NODE_ENV === "development" && (
              <pre className="mt-6 p-4 bg-muted rounded-md text-left text-sm overflow-auto max-h-48">
                {this.state.error?.message}
              </pre>
            )}
          </div>

          <SupportTicketDialog
            open={this.state.reportOpen}
            onClose={() => this.setState({ reportOpen: false })}
            presetCategory="Bug report"
            presetError={error ? { message: error.message, stack } : undefined}
          />
        </div>
      );
    }

    return this.props.children;
  }
}
