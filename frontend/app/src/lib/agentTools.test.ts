import { describe, expect, it } from 'vitest';
import { seedMcpMemberAccess, seedToolMemberAccess } from './agentTools';

describe('seedMcpMemberAccess (add a whole MCP — spec A1)', () => {
  it('allows members whose default is automated and greys restricted (ask/none) members', () => {
    const seeded = seedMcpMemberAccess([
      { capability: 'read', access_mode: 'automated' },
      { capability: 'write', access_mode: 'ask' },
      { capability: 'delete', access_mode: 'none' },
    ]);
    expect(seeded).toEqual({ read: 'automated', write: 'none', delete: 'none' });
  });

  it('treats an unknown default (e.g. a public/platform server) as allowed', () => {
    const seeded = seedMcpMemberAccess([
      { capability: 'a' },
      { capability: 'b', access_mode: null },
    ]);
    expect(seeded).toEqual({ a: 'automated', b: 'automated' });
  });
});

describe('seedToolMemberAccess (add a single tool)', () => {
  it('allows only the picked capability and greys every sibling', () => {
    const seeded = seedToolMemberAccess(
      [
        { capability: 'read', access_mode: 'automated' },
        { capability: 'write', access_mode: 'automated' },
      ],
      'read',
    );
    expect(seeded).toEqual({ read: 'automated', write: 'none' });
  });

  it('always includes the target even if it was absent from the member list', () => {
    expect(seedToolMemberAccess([], 'solo')).toEqual({ solo: 'automated' });
  });
});
