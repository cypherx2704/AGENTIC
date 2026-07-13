'use client';

import { useState } from 'react';
import Link from 'next/link';
import { Page, PageBody, PageHeader } from '@/components/AppShell';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  CopyButton,
  ErrorBanner,
  Loading,
  Modal,
  StatusBadge,
  Table,
  Textarea,
  useToast,
} from '@/components/ui';
import type { Column } from '@/components/ui';
import { useAsync } from '@/lib/useAsync';
import { listRestrictedSkills, listSkills, registerSkill } from '@/lib/services';
import type { SkillView } from '@/lib/types';
import { formatTime } from '@/lib/utils';

/** SkillView carries an index signature and permissive fields — coerce untyped values safely. */
function asString(v: unknown): string | undefined {
  return typeof v === 'string' && v.trim() ? v : undefined;
}

/** Owner is 'platform' | 'tenant' in the view; fall back to the is_platform flag if absent. */
function ownerOf(s: SkillView): 'platform' | 'tenant' {
  const o = asString(s.owner);
  if (o === 'platform' || o === 'tenant') return o;
  return s.is_platform ? 'platform' : 'tenant';
}

/** Prefer the resolved version, then the plain/latest version the view happens to carry. */
function versionOf(s: SkillView): string {
  return asString(s.resolved_version) ?? asString(s.version) ?? asString(s.latest_version) ?? '—';
}

function capabilityCount(s: SkillView): number {
  return Array.isArray(s.capabilities) ? s.capabilities.length : 0;
}

/** Health is a string in the discovery view but the type admits an object — handle both. */
function healthStatus(health: unknown): string | null {
  if (health === null || health === undefined) return null;
  if (typeof health === 'string') return health.trim() || null;
  if (typeof health === 'object') {
    const h = health as Record<string, unknown>;
    const s = h.status ?? h.state ?? h.health;
    return typeof s === 'string' ? s : 'unknown';
  }
  return String(health);
}

interface Catalog {
  skills: SkillView[];
  restricted: Array<Record<string, unknown>>;
}

/** The restricted list is best-effort — a failure there must not blank the main catalog. */
async function loadCatalog(signal: AbortSignal): Promise<Catalog> {
  const [skillsR, restrictedR] = await Promise.allSettled([listSkills(signal), listRestrictedSkills(signal)]);
  if (skillsR.status === 'rejected') throw skillsR.reason;
  return {
    skills: skillsR.value,
    restricted: restrictedR.status === 'fulfilled' ? (restrictedR.value.data ?? []) : [],
  };
}

export default function SkillsPage() {
  const toast = useToast();
  const { data, loading, error, reload } = useAsync(loadCatalog, []);

  const [registerOpen, setRegisterOpen] = useState(false);
  const [manifestText, setManifestText] = useState('');
  const [registering, setRegistering] = useState(false);

  const skills = data?.skills ?? [];
  const restricted = data?.restricted ?? [];

  async function onRegister() {
    let manifest: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(manifestText);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('the manifest must be a JSON object.');
      }
      manifest = parsed as Record<string, unknown>;
    } catch (err) {
      toast.error(err instanceof Error ? `Invalid manifest: ${err.message}` : 'Invalid JSON manifest.');
      return;
    }
    setRegistering(true);
    try {
      await registerSkill(manifest);
      toast.success('Skill registered.');
      setRegisterOpen(false);
      setManifestText('');
      reload();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Could not register the skill.');
    } finally {
      setRegistering(false);
    }
  }

  const columns: Array<Column<SkillView>> = [
    {
      key: 'name',
      header: 'Name',
      render: (s) => (
        <Link
          href={`/skills/${encodeURIComponent(s.name)}`}
          className="font-medium text-fg hover:text-brand hover:underline"
        >
          {s.name}
        </Link>
      ),
    },
    {
      key: 'owner',
      header: 'Owner',
      render: (s) => {
        const owner = ownerOf(s);
        return <Badge tone={owner === 'platform' ? 'info' : 'neutral'}>{owner === 'platform' ? 'Platform' : 'Tenant'}</Badge>;
      },
    },
    {
      key: 'version',
      header: 'Version',
      render: (s) => <span className="font-mono text-xs text-muted">{versionOf(s)}</span>,
    },
    {
      key: 'capabilities',
      header: 'Capabilities',
      className: 'text-right',
      render: (s) => <span className="font-mono text-xs tabular-nums text-fg">{capabilityCount(s)}</span>,
    },
    {
      key: 'health',
      header: 'Health',
      render: (s) => <StatusBadge status={healthStatus(s.health)} />,
    },
    {
      key: 'restricted',
      header: 'Restricted',
      render: (s) =>
        s.restricted === true ? <Badge tone="warning">Restricted</Badge> : <span className="text-xs text-muted">—</span>,
    },
  ];

  const restrictedColumns: Array<Column<Record<string, unknown>>> = [
    {
      key: 'name',
      header: 'Skill',
      render: (r) => {
        const nm = asString(r.name);
        if (nm) return <span className="font-medium text-fg">{nm}</span>;
        const id = asString(r.skill_id);
        return id ? <CopyButton value={id} label="Copy Skill ID" /> : <span className="text-muted">—</span>;
      },
    },
    {
      key: 'reason',
      header: 'Reason',
      render: (r) => <span className="text-muted">{asString(r.reason) ?? '—'}</span>,
    },
    {
      key: 'created_at',
      header: 'Restricted',
      className: 'text-right',
      render: (r) => <span className="text-xs text-muted">{formatTime(asString(r.created_at) ?? null)}</span>,
    },
  ];

  return (
    <Page>
      <PageHeader title="Skills" description="Reusable skills your agents can discover and run." />

      <PageBody className="flex flex-col gap-3">
        <Card>
          <CardHeader
            title="Skill Registry"
            actions={
              <>
                <Button variant="secondary" size="md" onClick={reload}>
                  Refresh
                </Button>
                <Button size="md" onClick={() => setRegisterOpen(true)}>
                  Register Skill
                </Button>
              </>
            }
          />
          <CardBody className="px-0 py-0">
            {error ? (
              <div className="p-4">
                <ErrorBanner error={error} title="Could not load skills" />
              </div>
            ) : loading ? (
              <Loading label="Loading skills…" />
            ) : (
              <Table
                columns={columns}
                rows={skills}
                rowKey={(s, i) => asString(s.skill_id) ?? `${s.name}-${i}`}
                empty="No skills registered yet. Register one to get started."
              />
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Restricted Skills" description="Skills that require explicit per-agent authorization." />
          <CardBody className="px-0 py-0">
            {error ? (
              <div className="p-4">
                <ErrorBanner error={error} title="Could not load restricted skills" />
              </div>
            ) : loading ? (
              <Loading label="Loading restricted skills…" />
            ) : (
              <Table
                columns={restrictedColumns}
                rows={restricted}
                rowKey={(r, i) => asString(r.skill_id) ?? `${asString(r.name) ?? 'skill'}-${i}`}
                empty="No skills are restricted."
              />
            )}
          </CardBody>
        </Card>
      </PageBody>

      <Modal
        open={registerOpen}
        onClose={() => (registering ? undefined : setRegisterOpen(false))}
        title="Register Skill"
        description="Paste a skill manifest (Contract-4 JSON). It is registered against your tenant."
        size="lg"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRegisterOpen(false)} disabled={registering}>
              Cancel
            </Button>
            <Button onClick={onRegister} loading={registering} disabled={!manifestText.trim()}>
              Register Skill
            </Button>
          </>
        }
      >
        <Textarea
          label="Manifest (JSON)"
          value={manifestText}
          onChange={(e) => setManifestText(e.target.value)}
          rows={14}
          spellCheck={false}
          placeholder='{ "name": "my-skill", "version": "1.0.0", "capabilities": [ … ] }'
          hint="Must be a JSON object. Parsing is validated before it is sent."
        />
      </Modal>
    </Page>
  );
}
