import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ExecutionTree } from './ExecutionTree';
import type { OrchestrationGraph, OrchestrationNode } from '@/lib/services';

function node(over: Partial<OrchestrationNode> & { node_id: string }): OrchestrationNode {
  return {
    node_type: 'agent',
    status: 'completed',
    depends_on: [],
    ...over,
  } as OrchestrationNode;
}

function graph(nodes: OrchestrationNode[], over: Partial<OrchestrationGraph['workflow']> = {}): OrchestrationGraph {
  return {
    workflow: {
      workflow_id: 'w',
      goal: 'do the thing',
      status: 'completed',
      mode: 'subagents',
      decomposition: 'llm',
      ...over,
    } as OrchestrationGraph['workflow'],
    nodes,
  };
}

describe('ExecutionTree', () => {
  it("shows a sub-agent's TOOL CALLS without having to expand the node", () => {
    // The whole point: you should be able to see which tools a sub-agent used at a glance.
    render(
      <ExecutionTree
        graph={graph([
          node({
            node_id: 'research',
            preset: 'gh-researcher',
            steps: [
              { step: 'llm_call', status: 'passed', step_type: 'llm_call', duration_ms: 500, tokens: 90 },
              { step: 'tool_call', status: 'passed', step_type: 'tool_call', tool: 'tool-github-stats', duration_ms: 120 },
              { step: 'tool_call', status: 'passed', step_type: 'tool_call', tool: 'tool-wikipedia', duration_ms: 80 },
            ],
          }),
        ])}
      />,
    );
    expect(screen.getByText('tool-github-stats')).toBeInTheDocument();
    expect(screen.getByText('tool-wikipedia')).toBeInTheDocument();
    // ...and they are counted in the run stats.
    expect(screen.getByText('Tool calls')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it("renders the sub-agent's full pipeline timeline on expand", async () => {
    const user = userEvent.setup();
    render(
      <ExecutionTree
        graph={graph([
          node({
            node_id: 'research',
            preset: 'gh-researcher',
            output: { summary: 'the findings' },
            steps: [
              { step: 'guardrail_check_input', status: 'passed', step_type: 'guardrail_check', duration_ms: 5 },
              { step: 'tool_call', status: 'passed', step_type: 'tool_call', tool: 'tool-github-stats', duration_ms: 120 },
              { step: 'guardrail_check_output', status: 'passed', step_type: 'guardrail_check', duration_ms: 4 },
            ],
          }),
        ])}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Details' }));

    // The same timeline the single-agent Task Runner shows.
    expect(screen.getByText('Guardrail (Input)')).toBeInTheDocument();
    expect(screen.getByText('Tool Call')).toBeInTheDocument();
    expect(screen.getByText('Guardrail (Output)')).toBeInTheDocument();
    expect(screen.getByText('the findings')).toBeInTheDocument();
  });

  it('groups independent nodes into ONE parallel wave, and dependents into the next', () => {
    // A fan-out must not read like a sequence — that was the old flat list's failure.
    render(
      <ExecutionTree
        graph={graph([
          node({ node_id: 'a', preset: 'researcher' }),
          node({ node_id: 'b', preset: 'researcher' }),
          node({ node_id: 'synth', preset: 'writer', depends_on: ['a', 'b'] }),
        ])}
      />,
    );
    expect(screen.getByText('Wave 1/2')).toBeInTheDocument();
    expect(screen.getByText('Wave 2/2')).toBeInTheDocument();
    expect(screen.getByText('2 in parallel')).toBeInTheDocument();
    expect(screen.getByText('depends on: a, b')).toBeInTheDocument();
  });

  it('labels a no-delegation run as the orchestrator answering itself', () => {
    render(<ExecutionTree graph={graph([node({ node_id: 'answer', preset: 'orchestrator' })])} />);
    expect(screen.getByText('orchestrator · no delegation')).toBeInTheDocument();
    expect(
      screen.getByText('The orchestrator answered this itself — no delegation was needed.'),
    ).toBeInTheDocument();
  });

  it('flags a failed tool call rather than hiding it', () => {
    render(
      <ExecutionTree
        graph={graph([
          node({
            node_id: 'research',
            status: 'failed',
            steps: [
              { step: 'tool_call', status: 'failed', step_type: 'tool_call', tool: 'tool-x', error: 'TOOL_DENIED', duration_ms: 3 },
            ],
          }),
        ])}
      />,
    );
    expect(screen.getByText('tool-x')).toBeInTheDocument();
  });

  it('does not hang or drop nodes when the graph has a cycle', () => {
    // Display data is not a validated graph; a cycle must still render every node.
    render(
      <ExecutionTree
        graph={graph([
          node({ node_id: 'a', depends_on: ['b'] }),
          node({ node_id: 'b', depends_on: ['a'] }),
        ])}
      />,
    );
    expect(screen.getByText('a')).toBeInTheDocument();
    expect(screen.getByText('b')).toBeInTheDocument();
  });

  it('shows a planning placeholder before any node exists', () => {
    render(<ExecutionTree graph={graph([])} />);
    expect(screen.getByText('Planning…')).toBeInTheDocument();
  });
});
