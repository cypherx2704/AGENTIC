import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ToastProvider } from '@/components/ui';
import { AgentBuilder } from './AgentBuilder';
import type { AgentRuntime } from '@/lib/types';
import { BffError } from '@/lib/bff-client';

const putRuntimeMock = vi.fn();
const setToolAccessMock = vi.fn();
const getToolAccessMock = vi.fn();
const listMcpsMock = vi.fn();
// The embedded AgentToolPicker loads MCPs/tools on mount and (in live mode) resolves per-tool
// access; stub those so the Builder tests exercise only the runtime config it owns.
vi.mock('@/lib/services', () => ({
  putRuntime: (...args: unknown[]) => putRuntimeMock(...args),
  listMcps: (...args: unknown[]) => listMcpsMock(...args),
  listBridgeTools: vi.fn(async () => []),
  listTools: vi.fn(async () => []),
  getToolAccess: (...args: unknown[]) => getToolAccessMock(...args),
  setToolAccess: (...args: unknown[]) => setToolAccessMock(...args),
}));

function makeRuntime(status: AgentRuntime['status']): AgentRuntime {
  return {
    agent_id: 'a1',
    tenant_id: 't1',
    name: 'Test Agent',
    runtime_version: '1.0.0',
    status,
    llm_model: 'smart',
    system_prompt: 'be helpful',
    max_tokens: 2048,
    temperature: 0.7,
    memory_scope: 'agent',
    guardrail_policy_id: null,
    allowed_tools: [],
    allowed_skills: [],
    allowed_kb_ids: [],
    rag_top_k_per_kb: 5,
    rag_min_score: 0.7,
    token_budget_per_task: 10000,
    capabilities: [],
    metadata: {},
  };
}

function renderBuilder(initial: AgentRuntime | null) {
  const onSaved = vi.fn();
  render(
    <ToastProvider>
      <AgentBuilder agentId="a1" fallbackName="Test Agent" initialRuntime={initial} models={[]} onSaved={onSaved} />
    </ToastProvider>,
  );
  return { onSaved };
}

describe('AgentBuilder', () => {
  beforeEach(() => {
    putRuntimeMock.mockReset();
    setToolAccessMock.mockReset().mockResolvedValue({});
    getToolAccessMock.mockReset().mockResolvedValue({ access_mode: 'none' });
    listMcpsMock.mockReset().mockResolvedValue([]);
  });

  it('exposes the full memory_scope enum including "session"', () => {
    renderBuilder(makeRuntime('active'));
    const select = screen.getByLabelText('Memory Scope') as HTMLSelectElement;
    const options = Array.from(select.options).map((o) => o.value);
    expect(options).toEqual(['none', 'agent', 'user', 'tenant', 'session']);
  });

  it('defaults the tool loop ON and saves tool_loop_enabled=true', async () => {
    const user = userEvent.setup();
    putRuntimeMock.mockResolvedValue(makeRuntime('pending_config'));
    // Runtime with no tool_loop_enabled (pre-0007) must default to the multi-call loop.
    renderBuilder(makeRuntime('pending_config'));

    const toggle = screen.getByRole('switch');
    expect(toggle.getAttribute('aria-checked')).toBe('true');

    await user.click(screen.getByRole('button', { name: /save config/i }));

    await waitFor(() => expect(putRuntimeMock).toHaveBeenCalledTimes(1));
    const body = putRuntimeMock.mock.calls[0][1] as { tool_loop_enabled: boolean };
    expect(body.tool_loop_enabled).toBe(true);
  });

  it('toggling the tool loop off saves tool_loop_enabled=false', async () => {
    const user = userEvent.setup();
    putRuntimeMock.mockResolvedValue(makeRuntime('pending_config'));
    renderBuilder(makeRuntime('pending_config'));

    await user.click(screen.getByRole('switch'));
    await user.click(screen.getByRole('button', { name: /save config/i }));

    await waitFor(() => expect(putRuntimeMock).toHaveBeenCalledTimes(1));
    const body = putRuntimeMock.mock.calls[0][1] as { tool_loop_enabled: boolean };
    expect(body.tool_loop_enabled).toBe(false);
  });

  it('reflects an existing tool_loop_enabled=false runtime in the switch', () => {
    renderBuilder({ ...makeRuntime('active'), tool_loop_enabled: false });
    const toggle = screen.getByRole('switch');
    expect(toggle.getAttribute('aria-checked')).toBe('false');
  });

  it('saving config calls putRuntime once with the form values', async () => {
    const user = userEvent.setup();
    putRuntimeMock.mockResolvedValue(makeRuntime('pending_config'));
    const { onSaved } = renderBuilder(makeRuntime('pending_config'));

    await user.click(screen.getByRole('button', { name: /save config/i }));

    await waitFor(() => expect(putRuntimeMock).toHaveBeenCalledTimes(1));
    expect(onSaved).toHaveBeenCalled();
  });

  it('publish runs step 1 (save) then step 2 (activate)', async () => {
    const user = userEvent.setup();
    putRuntimeMock
      .mockResolvedValueOnce(makeRuntime('pending_config')) // step 1 save
      .mockResolvedValueOnce(makeRuntime('active')); // step 2 activate
    renderBuilder(makeRuntime('pending_config'));

    await user.click(screen.getByRole('button', { name: /^publish$/i }));

    await waitFor(() => expect(putRuntimeMock).toHaveBeenCalledTimes(2));
    // Step 2 must request status=active.
    const secondCallBody = putRuntimeMock.mock.calls[1][1] as { status: string };
    expect(secondCallBody.status).toBe('active');
  });

  it('stages picker grants and flushes EVERY member (incl. explicit none) to setToolAccess after putRuntime', async () => {
    const user = userEvent.setup();
    // An attached MCP with two members; both start allowed (hydrated as automated).
    listMcpsMock.mockResolvedValue([
      {
        mcp_id: 'm1',
        slug: 'mcp-x',
        server_name: 'mcp-x',
        display_name: 'X',
        description: '',
        visibility: 'private',
        status: 'active',
        version: '1',
        tools: [
          { tool_id: 't1', snake_name: 'read', display_name: 'Read' },
          { tool_id: 't2', snake_name: 'write', display_name: 'Write' },
        ],
      },
    ]);
    getToolAccessMock.mockResolvedValue({ access_mode: 'automated' });
    putRuntimeMock.mockResolvedValue({ ...makeRuntime('active'), allowed_tools: ['mcp-x'] });
    renderBuilder({ ...makeRuntime('active'), allowed_tools: ['mcp-x'] });

    // Wait for hydration, then deny the "write" sibling (stages none) — nothing persists yet.
    const writeBtn = await screen.findByRole('button', { name: /write/i });
    expect(setToolAccessMock).not.toHaveBeenCalled();
    await user.click(writeBtn);
    expect(setToolAccessMock).not.toHaveBeenCalled();

    // Save flushes the runtime first, then the FULL staged access map to the registry.
    await user.click(screen.getByRole('button', { name: /save config/i }));
    await waitFor(() => expect(putRuntimeMock).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(setToolAccessMock).toHaveBeenCalled());

    const grants = setToolAccessMock.mock.calls.map(([server, body]) => ({ server, ...body }));
    expect(grants).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ server: 'mcp-x', capability: 'read', access_mode: 'automated' }),
        expect.objectContaining({ server: 'mcp-x', capability: 'write', access_mode: 'none' }),
      ]),
    );
  });

  it('on step-2 failure shows a "Retry publish (step 2)" button that re-attempts only activation', async () => {
    const user = userEvent.setup();
    putRuntimeMock
      .mockResolvedValueOnce(makeRuntime('pending_config')) // step 1 succeeds
      .mockRejectedValueOnce(new BffError(503, { code: 'SERVICE_UNAVAILABLE', message: 'downstream down' })) // step 2 fails
      .mockResolvedValueOnce(makeRuntime('active')); // retry step 2 succeeds
    renderBuilder(makeRuntime('pending_config'));

    await user.click(screen.getByRole('button', { name: /^publish$/i }));

    // The retry button appears after the step-2 failure.
    const retry = await screen.findByRole('button', { name: /retry publish/i });
    expect(putRuntimeMock).toHaveBeenCalledTimes(2);

    await user.click(retry);
    await waitFor(() => expect(putRuntimeMock).toHaveBeenCalledTimes(3));
    // The retry call activates (status=active) — it does NOT re-run a separate save first.
    const retryBody = putRuntimeMock.mock.calls[2][1] as { status: string };
    expect(retryBody.status).toBe('active');
  });
});
