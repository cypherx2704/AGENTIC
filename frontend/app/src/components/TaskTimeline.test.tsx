import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TaskTimeline } from './TaskTimeline';

describe('TaskTimeline', () => {
  it('renders the ordered canonical steps with friendly labels', () => {
    render(
      <TaskTimeline
        steps={[
          { step: 'guardrail_check_input', status: 'passed', duration_ms: 12, tokens: null },
          { step: 'llm_call', status: 'completed', duration_ms: 640, tokens: 128 },
          { step: 'guardrail_check_output', status: 'passed', duration_ms: 9, tokens: null },
        ]}
      />,
    );
    expect(screen.getByText('Guardrail (input)')).toBeInTheDocument();
    expect(screen.getByText('LLM call')).toBeInTheDocument();
    expect(screen.getByText('Guardrail (output)')).toBeInTheDocument();
    expect(screen.getByText('128 tok')).toBeInTheDocument();
  });

  it('shows an empty state when there are no steps', () => {
    render(<TaskTimeline steps={[]} />);
    expect(screen.getByText(/no steps recorded/i)).toBeInTheDocument();
  });
});
