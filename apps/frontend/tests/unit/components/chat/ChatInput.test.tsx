import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// ChatInput consumes useSlashCommands (gateway-backed) for the slash menu
// (#1062). These tests render without a GatewayProvider, so stub the hook to
// an empty command set — none of these cases exercise the menu. Mirrors the
// stub in src/components/chat/__tests__/ChatInput.test.tsx.
vi.mock('@/hooks/useSlashCommands', () => ({
  useSlashCommands: (): { commands: never[]; isLoading: boolean } => ({
    commands: [],
    isLoading: false,
  }),
}));

import { ChatInput } from '@/components/chat/ChatInput';

describe('ChatInput', () => {
  const mockOnSend = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  function getTextarea(): HTMLElement {
    return screen.getByPlaceholderText('Ask anything');
  }

  function getSendButton(): HTMLElement {
    return screen.getByTestId('send-button');
  }

  describe('rendering', () => {
    it('renders textarea and send button', () => {
      render(<ChatInput onSend={mockOnSend} />);

      expect(getTextarea()).toBeInTheDocument();
      expect(getSendButton()).toBeInTheDocument();
    });

    // The composer redesign (#1123) made the card chrome unconditional — the
    // conversation-mode cream band on the wrapper is gone and the card floats
    // on the canvas. `centered` now only toggles the wrapper's top padding.
    it('applies the docked top padding when not centered', () => {
      const { container } = render(<ChatInput onSend={mockOnSend} />);
      expect(container.firstChild).toHaveClass('pt-1');
    });

    it('omits the docked top padding when centered', () => {
      const { container } = render(<ChatInput onSend={mockOnSend} centered />);
      expect(container.firstChild).not.toHaveClass('pt-1');
    });
  });

  describe('sending messages', () => {
    it('calls onSend with input value on button click', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      await user.type(getTextarea(), 'Hello world');
      await user.click(getSendButton());

      expect(mockOnSend).toHaveBeenCalledWith('Hello world', undefined);
    });

    it('clears input after send', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      const textarea = getTextarea();
      await user.type(textarea, 'Test message');
      await user.click(getSendButton());

      expect(textarea).toHaveValue('');
    });

    it('sends on Enter key', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      await user.type(getTextarea(), 'Enter test');
      await user.keyboard('{Enter}');

      expect(mockOnSend).toHaveBeenCalledWith('Enter test', undefined);
    });

    it('does not send on Shift+Enter', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      await user.type(getTextarea(), 'Shift enter test');
      await user.keyboard('{Shift>}{Enter}{/Shift}');

      expect(mockOnSend).not.toHaveBeenCalled();
    });
  });

  describe('validation', () => {
    it('does not send when input is empty', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      await user.click(getSendButton());

      expect(mockOnSend).not.toHaveBeenCalled();
    });

    it('does not send when input is only whitespace', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      await user.type(getTextarea(), '   ');
      await user.click(getSendButton());

      expect(mockOnSend).not.toHaveBeenCalled();
    });
  });

  describe('disabled state', () => {
    it('disables textarea when disabled', () => {
      render(<ChatInput onSend={mockOnSend} disabled />);
      expect(getTextarea()).toBeDisabled();
    });

    it('disables send button when disabled', () => {
      render(<ChatInput onSend={mockOnSend} disabled />);
      expect(getSendButton()).toBeDisabled();
    });

    it('disables send button when input is empty', () => {
      render(<ChatInput onSend={mockOnSend} />);
      expect(getSendButton()).toBeDisabled();
    });

    it('enables send button when input has content', async () => {
      const user = userEvent.setup();
      render(<ChatInput onSend={mockOnSend} />);

      await user.type(getTextarea(), 'Test');

      expect(getSendButton()).not.toBeDisabled();
    });
  });
});
