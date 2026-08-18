import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Mock ProviderIcons — return simple SVG components for each provider
vi.mock('@/components/chat/ProviderIcons', () => {
  const createIcon = (name: string) => {
    const Icon = ({ size = 16, ...props }: { size?: number; [key: string]: unknown }) =>
      <svg data-testid={`icon-${name}`} width={size} height={size} {...props} />;
    Icon.displayName = name;
    return Icon;
  };
  return {
    Ai21Icon: createIcon('ai21'),
    AnthropicIcon: createIcon('anthropic'),
    AwsIcon: createIcon('aws'),
    CohereIcon: createIcon('cohere'),
    DeepSeekIcon: createIcon('deepseek'),
    GoogleIcon: createIcon('google'),
    MetaIcon: createIcon('meta'),
    MinimaxIcon: createIcon('minimax'),
    MistralIcon: createIcon('mistral'),
    MoonshotIcon: createIcon('moonshot'),
    NvidiaIcon: createIcon('nvidia'),
    OpenAIIcon: createIcon('openai'),
    QwenIcon: createIcon('qwen'),
    ZhipuIcon: createIcon('zhipu'),
  };
});

import { ModelSelector } from '@/components/chat/ModelSelector';

const mockModels = [
  { id: 'us.anthropic.claude-3-5-sonnet-v2:0', name: 'Claude 3.5 Sonnet' },
  { id: 'us.anthropic.claude-3-5-haiku-v1:0', name: 'Claude 3.5 Haiku' },
  { id: 'us.meta.llama3-3-70b-v1:0', name: 'Llama 3.3 70B' },
];

describe('ModelSelector', () => {
  const mockOnModelChange = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  function renderSelector(props: Partial<Parameters<typeof ModelSelector>[0]> = {}): void {
    render(
      <ModelSelector
        models={mockModels}
        selectedModel="us.anthropic.claude-3-5-sonnet-v2:0"
        onModelChange={mockOnModelChange}
        {...props}
      />
    );
  }

  describe('rendering', () => {
    it('renders selected model name', () => {
      renderSelector();
      expect(screen.getByText('Claude 3.5 Sonnet')).toBeInTheDocument();
    });

    it('shows placeholder when no match found', () => {
      renderSelector({ selectedModel: 'nonexistent' });
      expect(screen.getByText('Select Model')).toBeInTheDocument();
    });

    it('shows placeholder when models array is empty', () => {
      renderSelector({ models: [], selectedModel: '' });
      expect(screen.getByText('Select Model')).toBeInTheDocument();
    });

    it('disables button when disabled prop is true', () => {
      renderSelector({ disabled: true });
      expect(screen.getByRole('button')).toBeDisabled();
    });
  });

  describe('dropdown behavior', () => {
    it('opens dropdown and shows provider group headers', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      expect(screen.getByText('Anthropic')).toBeInTheDocument();
      expect(screen.getByText('Meta')).toBeInTheDocument();
    });

    it('shows correct model count badges per group', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      // Anthropic has 2 models, Meta has 1
      expect(screen.getByText('2')).toBeInTheDocument();
      expect(screen.getByText('1')).toBeInTheDocument();
    });

    it('auto-expands selected model group on open', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      // Anthropic group should be auto-expanded (contains selected model)
      // Both Anthropic models should be visible
      const sonnetElements = screen.getAllByText('Claude 3.5 Sonnet');
      expect(sonnetElements.length).toBeGreaterThanOrEqual(1);
      expect(screen.getByText('Claude 3.5 Haiku')).toBeInTheDocument();

      // Meta group should be collapsed — Llama should NOT be visible as a model row
      // But "Meta" header should still be visible
      expect(screen.getByText('Meta')).toBeInTheDocument();
    });

    it('toggles group expand/collapse on header click', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      // Meta is collapsed — click to expand
      await user.click(screen.getByText('Meta'));
      expect(screen.getByText('Llama 3.3 70B')).toBeInTheDocument();

      // Click again to collapse
      await user.click(screen.getByText('Meta'));
      // The model row should no longer be visible (collapsed)
      expect(screen.queryByText('Llama 3.3 70B')).not.toBeInTheDocument();
    });

    it('renders all models when groups are expanded', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      // Expand Meta group (Anthropic is auto-expanded)
      await user.click(screen.getByText('Meta'));

      for (const model of mockModels) {
        const elements = screen.getAllByText(model.name);
        expect(elements.length).toBeGreaterThanOrEqual(1);
      }
    });

    it('calls onModelChange when model selected', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      // Anthropic is auto-expanded, click Haiku
      await user.click(screen.getByText('Claude 3.5 Haiku'));

      expect(mockOnModelChange).toHaveBeenCalledWith('us.anthropic.claude-3-5-haiku-v1:0');
    });

    it('calls onModelChange from collapsed group after expanding', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      // Expand Meta group
      await user.click(screen.getByText('Meta'));
      await user.click(screen.getByText('Llama 3.3 70B'));

      expect(mockOnModelChange).toHaveBeenCalledWith('us.meta.llama3-3-70b-v1:0');
    });
  });

  describe('search', () => {
    it('filters models by name and auto-expands all groups', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      const searchInput = screen.getByPlaceholderText('Search models...');
      await user.type(searchInput, 'Llama');

      // Meta group should appear with Llama visible (auto-expanded)
      expect(screen.getByText('Llama 3.3 70B')).toBeInTheDocument();
      // Anthropic group should be gone (no match)
      expect(screen.queryByText('Anthropic')).not.toBeInTheDocument();
    });

    it('filters models by ID', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      const searchInput = screen.getByPlaceholderText('Search models...');
      await user.type(searchInput, 'haiku');

      expect(screen.getByText('Claude 3.5 Haiku')).toBeInTheDocument();
      // Sonnet still appears in the trigger button, but should not appear as a dropdown row
      // Only 1 instance of "Claude 3.5 Sonnet" should remain (the trigger)
      const sonnetElements = screen.getAllByText('Claude 3.5 Sonnet');
      expect(sonnetElements).toHaveLength(1); // only the trigger button
    });

    it('shows no models found when search has no matches', async () => {
      const user = userEvent.setup();
      renderSelector();

      await user.click(screen.getByRole('button', { name: /Claude 3\.5 Sonnet/i }));

      const searchInput = screen.getByPlaceholderText('Search models...');
      await user.type(searchInput, 'zzzzz');

      expect(screen.getByText('No models found')).toBeInTheDocument();
    });
  });
});
