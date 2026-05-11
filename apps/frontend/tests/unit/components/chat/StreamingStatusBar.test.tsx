import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  StreamingStatusBar,
  deriveStreamingLabel,
} from '@/components/chat/StreamingStatusBar';
import type { Message, ToolUse } from '@/components/chat/MessageList';

function assistantMsg(id: string, toolUses?: ToolUse[]): Message {
  return { id, role: 'assistant', content: '', ...(toolUses ? { toolUses } : {}) };
}

describe('StreamingStatusBar', () => {
  it('renders nothing when not streaming', () => {
    const { container } = render(
      <StreamingStatusBar isStreaming={false} messages={[]} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows "Thinking…" when streaming with no messages', () => {
    render(<StreamingStatusBar isStreaming={true} messages={[]} />);
    expect(screen.getByTestId('streaming-status-bar')).toBeInTheDocument();
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
  });

  it('shows "Thinking…" when only finished tools exist', () => {
    const messages = [
      assistantMsg('a1', [
        { tool: 'shell', status: 'done' },
        { tool: 'fs.read', status: 'done' },
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} messages={messages} />);
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
  });

  it('shows "Running <tool>" when a tool is running on the last assistant bubble', () => {
    const messages = [
      assistantMsg('a1', [
        { tool: 'fs.read', status: 'done' },
        { tool: 'shell', status: 'running' },
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} messages={messages} />);
    expect(screen.getByText(/Running shell/)).toBeInTheDocument();
  });

  it('prefers the most recent running tool on the last bubble', () => {
    const messages = [
      assistantMsg('a1', [
        { tool: 'shell', status: 'running' },
        { tool: 'fs.read', status: 'running' },
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} messages={messages} />);
    expect(screen.getByText(/Running fs\.read/)).toBeInTheDocument();
  });

  // Removed: three "surfaces pending-approval" tests. Approvals are no
  // longer in messages.toolUses — they live in the global queue managed
  // by useExecApprovals and render via the modal ExecApprovalDialog.
  // StreamingStatusBar now only reports running execution / Thinking.

  it('only counts running tools from the most recent assistant bubble', () => {
    const messages = [
      assistantMsg('a1', [{ tool: 'old-tool', status: 'running' }]),
      assistantMsg('a2', [{ tool: 'fresh-tool', status: 'running' }]),
    ];
    render(<StreamingStatusBar isStreaming={true} messages={messages} />);
    expect(screen.getByText(/Running fresh-tool/)).toBeInTheDocument();
  });

  it('exports a pure deriveStreamingLabel helper', () => {
    expect(deriveStreamingLabel([])).toBe('Thinking');
    expect(
      deriveStreamingLabel([
        assistantMsg('a1', [{ tool: 'exec', status: 'running' }]),
      ])
    ).toBe('Running exec');
  });
});
