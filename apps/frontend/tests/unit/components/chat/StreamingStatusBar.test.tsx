import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StreamingStatusBar } from '@/components/chat/StreamingStatusBar';
import type { ToolUse } from '@/components/chat/MessageList';

describe('StreamingStatusBar', () => {
  it('renders nothing when not streaming', () => {
    const { container } = render(<StreamingStatusBar isStreaming={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows "Thinking…" when streaming with no tool uses', () => {
    render(<StreamingStatusBar isStreaming={true} />);
    expect(screen.getByTestId('streaming-status-bar')).toBeInTheDocument();
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
  });

  it('shows "Thinking…" when streaming with only finished tools', () => {
    const toolUses: ToolUse[] = [
      { tool: 'shell', status: 'done' },
      { tool: 'fs.read', status: 'done' },
    ];
    render(<StreamingStatusBar isStreaming={true} toolUses={toolUses} />);
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
  });

  it('shows "Running <tool>" when a tool is running', () => {
    const toolUses: ToolUse[] = [
      { tool: 'fs.read', status: 'done' },
      { tool: 'shell', status: 'running' },
    ];
    render(<StreamingStatusBar isStreaming={true} toolUses={toolUses} />);
    expect(screen.getByText(/Running shell/)).toBeInTheDocument();
  });

  it('prefers the most recent running tool', () => {
    const toolUses: ToolUse[] = [
      { tool: 'shell', status: 'running' },
      { tool: 'fs.read', status: 'running' },
    ];
    render(<StreamingStatusBar isStreaming={true} toolUses={toolUses} />);
    expect(screen.getByText(/Running fs\.read/)).toBeInTheDocument();
  });

  it('surfaces pending-approval state over earlier running tool', () => {
    const toolUses: ToolUse[] = [
      { tool: 'shell', status: 'running' },
      { tool: 'exec', status: 'pending-approval' },
    ];
    render(<StreamingStatusBar isStreaming={true} toolUses={toolUses} />);
    expect(screen.getByText(/Awaiting approval: exec/)).toBeInTheDocument();
  });

  it('surfaces pending-approval even when a newer tool is still running', () => {
    // Order matters: approval first, then running. The label must still
    // prefer the blocking approval — otherwise the user can't see what's
    // holding things up.
    const toolUses: ToolUse[] = [
      { tool: 'exec', status: 'pending-approval' },
      { tool: 'shell', status: 'running' },
    ];
    render(<StreamingStatusBar isStreaming={true} toolUses={toolUses} />);
    expect(screen.getByText(/Awaiting approval: exec/)).toBeInTheDocument();
  });
});
