import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MessageList } from '@/components/chat/MessageList';

const mockMessages = [
  { id: '1', role: 'user' as const, content: 'Hello there!' },
  { id: '2', role: 'assistant' as const, content: 'Hi! How can I help you?' },
  { id: '3', role: 'user' as const, content: 'What is the weather?' },
];

describe('MessageList', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  describe('rendering', () => {
    it('renders all messages', () => {
      render(<MessageList messages={mockMessages} />);

      expect(screen.getByText('Hello there!')).toBeInTheDocument();
      expect(screen.getByText('Hi! How can I help you?')).toBeInTheDocument();
      expect(screen.getByText('What is the weather?')).toBeInTheDocument();
    });

    it('renders container when no messages', () => {
      const { container } = render(<MessageList messages={[]} />);
      expect(container.querySelector('.space-y-10')).toBeInTheDocument();
    });

    it('preserves whitespace in messages', () => {
      const multilineContent = 'Line 1\nLine 2\nLine 3';
      const { container } = render(
        <MessageList messages={[{ id: '1', role: 'user', content: multilineContent }]} />
      );

      const messageElement = container.querySelector('.whitespace-pre-wrap');
      expect(messageElement).toBeInTheDocument();
      expect(messageElement).toHaveTextContent('Line 1');
      expect(messageElement).toHaveTextContent('Line 2');
      expect(messageElement).toHaveTextContent('Line 3');
    });
  });

  describe('message alignment', () => {
    it('aligns user messages to the right', () => {
      render(<MessageList messages={[{ id: '1', role: 'user', content: 'User message' }]} />);

      const messageWrapper = screen.getByText('User message').closest('[data-role="user"]');
      expect(messageWrapper).toHaveClass('justify-end');
    });

    it('aligns assistant messages to the left', () => {
      render(<MessageList messages={[{ id: '1', role: 'assistant', content: 'Assistant message' }]} />);

      const messageWrapper = screen.getByText('Assistant message').closest('[data-role="assistant"]');
      expect(messageWrapper).toHaveClass('justify-start');
    });
  });

  describe('message styling', () => {
    it('applies correct text style to user messages', () => {
      render(<MessageList messages={[{ id: '1', role: 'user', content: 'User message' }]} />);

      const messageText = screen.getByText('User message').closest('.text-sm');
      expect(messageText).toHaveClass('bg-[#f0ebe2]');
    });

    it('applies correct text style to assistant messages', () => {
      render(<MessageList messages={[{ id: '1', role: 'assistant', content: 'Assistant message' }]} />);

      const messageText = screen.getByText('Assistant message').closest('.text-sm');
      expect(messageText).toBeInTheDocument();
    });
  });

  describe('typing indicator', () => {
    it('shows the thinking glyph on an existing empty assistant bubble when isTyping', () => {
      render(
        <MessageList
          messages={[{ id: '1', role: 'assistant', content: '' }]}
          isTyping={true}
        />
      );

      const thinking = document.querySelectorAll('.agent-glyph--thinking');
      expect(thinking.length).toBe(1);
    });

    it('hides when not typing', () => {
      render(
        <MessageList
          messages={[{ id: '1', role: 'assistant', content: '' }]}
          isTyping={false}
        />
      );

      const thinking = document.querySelectorAll('.agent-glyph--thinking');
      expect(thinking.length).toBe(0);
    });

    it('does NOT attach to user messages', () => {
      render(
        <MessageList
          messages={[{ id: '1', role: 'user', content: 'hi' }]}
          isTyping={false}
        />
      );

      // No assistant bubble exists, no typing either → nothing to show.
      expect(document.querySelectorAll('.agent-glyph--thinking').length).toBe(0);
      expect(screen.queryByTestId('typing-placeholder')).not.toBeInTheDocument();
    });
  });

  describe('typing placeholder (pre-stream header)', () => {
    // Fixes "I don't see the header loading while the response is coming at
    // first" — multi-bubble rendering creates assistant bubbles lazily on
    // first chunk, so the window between sendMessage and first chunk had no
    // AgentHead to animate. The placeholder fills that gap.

    it('shows when last message is user AND isTyping', () => {
      render(
        <MessageList
          messages={[{ id: '1', role: 'user', content: 'What is the weather?' }]}
          isTyping={true}
          agentName="Robbie"
        />
      );

      expect(screen.getByTestId('typing-placeholder')).toBeInTheDocument();
      // The placeholder renders an AgentHead with state="thinking" —
      // verify the animated glyph is there and the agent name is shown.
      const placeholder = screen.getByTestId('typing-placeholder');
      expect(placeholder.querySelector('.agent-glyph--thinking')).toBeInTheDocument();
      expect(placeholder).toHaveTextContent('Robbie');
    });

    it('shows when there are no messages yet AND isTyping', () => {
      render(<MessageList messages={[]} isTyping={true} />);
      expect(screen.getByTestId('typing-placeholder')).toBeInTheDocument();
    });

    it('hides once an assistant bubble exists (even if still streaming)', () => {
      render(
        <MessageList
          messages={[
            { id: '1', role: 'user', content: 'hi' },
            { id: '2', role: 'assistant', content: 'Hello' },
          ]}
          isTyping={true}
        />
      );

      // Assistant bubble is present → the real AgentHead carries the
      // thinking state; the placeholder stays hidden.
      expect(screen.queryByTestId('typing-placeholder')).not.toBeInTheDocument();
      expect(document.querySelectorAll('.agent-glyph--thinking').length).toBe(1);
    });

    it('hides when isTyping is false', () => {
      render(
        <MessageList
          messages={[{ id: '1', role: 'user', content: 'hi' }]}
          isTyping={false}
        />
      );
      expect(screen.queryByTestId('typing-placeholder')).not.toBeInTheDocument();
    });
  });

  describe('auto-scroll', () => {
    it('renders scroll anchor element at end of messages', () => {
      const { container } = render(
        <MessageList messages={[{ id: '1', role: 'user', content: 'Test message' }]} />
      );

      const scrollContainer = container.querySelector('[data-lenis-prevent]');
      expect(scrollContainer).toBeInTheDocument();

      const messageContainer = container.querySelector('.space-y-10');
      expect(messageContainer).toBeInTheDocument();
      expect(messageContainer?.lastElementChild?.tagName).toBe('DIV');
    });

    it('scrolls to bottom on first paint with messages (agent-entry / history load)', () => {
      const scrollSpy = vi.fn();
      Element.prototype.scrollIntoView = scrollSpy;

      render(<MessageList messages={mockMessages} />);

      // The effect should have fired the anchor's scrollIntoView at least
      // once for the initial paint. Assert behavior: "auto" (no animation)
      // so long histories don't animate.
      expect(scrollSpy).toHaveBeenCalled();
      expect(scrollSpy).toHaveBeenCalledWith(
        expect.objectContaining({ behavior: 'auto' }),
      );
    });

    it('scrolls when a new user message is added (force-scroll)', () => {
      const scrollSpy = vi.fn();
      Element.prototype.scrollIntoView = scrollSpy;

      const { rerender } = render(
        <MessageList messages={[{ id: '1', role: 'assistant', content: 'hi' }]} />
      );
      scrollSpy.mockClear();

      rerender(
        <MessageList
          messages={[
            { id: '1', role: 'assistant', content: 'hi' },
            { id: '2', role: 'user', content: 'hey' },
          ]}
        />
      );

      expect(scrollSpy).toHaveBeenCalled();
    });

    it('scrolls when a SECOND user message is sent back-to-back (tail role stays "user")', () => {
      // Multi-bubble sendMessage appends only a user row — so two sends
      // in a row (no assistant response between) leave the tail role as
      // user. The force-scroll rule must not gate on role *changing*.
      const scrollSpy = vi.fn();
      Element.prototype.scrollIntoView = scrollSpy;

      const { rerender } = render(
        <MessageList messages={[{ id: '1', role: 'user', content: 'first' }]} />
      );
      scrollSpy.mockClear();

      rerender(
        <MessageList
          messages={[
            { id: '1', role: 'user', content: 'first' },
            { id: '2', role: 'user', content: 'second' },
          ]}
        />
      );

      expect(scrollSpy).toHaveBeenCalled();
    });

    it('scrolls when streamed content grows on the tail assistant bubble', () => {
      const scrollSpy = vi.fn();
      Element.prototype.scrollIntoView = scrollSpy;

      const { rerender } = render(
        <MessageList messages={[{ id: '1', role: 'assistant', content: 'A' }]} />
      );
      scrollSpy.mockClear();

      rerender(
        <MessageList messages={[{ id: '1', role: 'assistant', content: 'A longer streamed response' }]} />
      );

      // JSDOM defaults scrollHeight / scrollTop / clientHeight to 0, which
      // yields distance = 0 → isNearBottom = true → we scroll.
      expect(scrollSpy).toHaveBeenCalled();
    });
  });
});

// Removed: "MessageList approval rendering" describe block. Approvals
// are no longer surfaced inline on tool bubbles — they live in the
// global queue managed by useExecApprovals and render via the modal
// ExecApprovalDialog mounted at the chat-shell level. ToolUse no longer
// has "pending-approval" or "denied" statuses; MessageList no longer
// accepts an `onDecide` prop. Coverage for the new flow belongs in
// useExecApprovals + ExecApprovalDialog tests (follow-up).
