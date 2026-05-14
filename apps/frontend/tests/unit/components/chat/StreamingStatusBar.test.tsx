import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  StreamingStatusBar,
  deriveStreamingLabel,
} from '@/components/chat/StreamingStatusBar';
import type { ChatRenderItem, ContentBlock } from '@/hooks/internal/types';

// Helper: build a {kind:"message"} ChatRenderItem for an assistant turn
// whose content[] is the supplied list of content blocks. The new
// deriveStreamingLabel walks the trailing assistant message looking for
// tool_use blocks without a matching tool_result.
function assistantItem(key: string, content: ContentBlock[]): ChatRenderItem {
  return {
    kind: 'message',
    key,
    message: { role: 'assistant', content },
  };
}

describe('StreamingStatusBar', () => {
  it('renders nothing when not streaming', () => {
    const { container } = render(
      <StreamingStatusBar isStreaming={false} renderItems={[]} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows "Thinking…" when streaming with no render items', () => {
    render(<StreamingStatusBar isStreaming={true} renderItems={[]} />);
    expect(screen.getByTestId('streaming-status-bar')).toBeInTheDocument();
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
  });

  it('shows "Thinking…" when every tool_use has a matching tool_result', () => {
    const items: ChatRenderItem[] = [
      assistantItem('a1', [
        { type: 'tool_use', id: 't1', name: 'shell' },
        { type: 'tool_result', tool_use_id: 't1', content: 'ok' },
        { type: 'tool_use', id: 't2', name: 'fs.read' },
        { type: 'tool_result', tool_use_id: 't2', content: 'ok' },
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} renderItems={items} />);
    expect(screen.getByText(/Thinking/)).toBeInTheDocument();
  });

  it('shows "Running <tool>" when a tool_use on the last assistant item is unpaired', () => {
    const items: ChatRenderItem[] = [
      assistantItem('a1', [
        { type: 'tool_use', id: 't1', name: 'fs.read' },
        { type: 'tool_result', tool_use_id: 't1', content: 'ok' },
        { type: 'tool_use', id: 't2', name: 'shell' },
        // no tool_result for t2 — still running
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} renderItems={items} />);
    expect(screen.getByText(/Running shell/)).toBeInTheDocument();
  });

  it('prefers the most recent running tool on the trailing assistant item', () => {
    const items: ChatRenderItem[] = [
      assistantItem('a1', [
        { type: 'tool_use', id: 't1', name: 'shell' }, // running
        { type: 'tool_use', id: 't2', name: 'fs.read' }, // running
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} renderItems={items} />);
    expect(screen.getByText(/Running fs\.read/)).toBeInTheDocument();
  });

  it('only counts running tools from the most recent assistant message', () => {
    const items: ChatRenderItem[] = [
      assistantItem('a1', [
        { type: 'tool_use', id: 't-old', name: 'old-tool' }, // unpaired in old turn
      ]),
      {
        kind: 'message',
        key: 'u1',
        message: { role: 'user', content: [{ type: 'text', text: 'next' }] },
      },
      assistantItem('a2', [
        { type: 'tool_use', id: 't-fresh', name: 'fresh-tool' },
      ]),
    ];
    render(<StreamingStatusBar isStreaming={true} renderItems={items} />);
    expect(screen.getByText(/Running fresh-tool/)).toBeInTheDocument();
  });

  it('exports a pure deriveStreamingLabel helper', () => {
    expect(deriveStreamingLabel([])).toBe('Thinking');
    expect(
      deriveStreamingLabel([
        assistantItem('a1', [
          { type: 'tool_use', id: 't1', name: 'exec' },
        ]),
      ])
    ).toBe('Running exec');
  });
});
