from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "desktop" / "plugin.js"
DOCS = ROOT / "docs" / "desktop.md"
TESTS = ROOT / "tests" / "unit" / "test_desktop_plugin_assets.py"


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise RuntimeError(f"expected exactly one anchor in {path}: {old[:80]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


membership_block = r"""
export const MEMBERSHIP_FILTERS = Object.freeze([
  Object.freeze(['all', 'All']),
  Object.freeze(['managed', 'Managed']),
  Object.freeze(['active', 'Active']),
  Object.freeze(['not_active', 'Not active']),
  Object.freeze(['disabled', 'Disabled']),
  Object.freeze(['removed', 'Removed']),
  Object.freeze(['ready', 'Ready']),
  Object.freeze(['attention', 'Attention']),
  Object.freeze(['observed', 'Observed'])
])

function membershipManagedStatus(node) {
  if (node.managed.state === 'removed') {
    return { key: 'removed', label: 'Removed', tone: 'muted' }
  }
  if (node.managed.state === 'disabled') {
    return { key: 'disabled', label: 'Disabled', tone: 'muted' }
  }
  if (node.readiness.scheduler_ready) {
    return { key: 'ready', label: 'Ready', tone: 'good' }
  }
  if (node.readiness.alive) {
    return { key: 'attention', label: 'Needs attention', tone: 'warn' }
  }
  return { key: 'awaiting', label: 'Awaiting evidence', tone: 'bad' }
}

function membershipSearchText(values) {
  return normalizedSearch(values.filter(Boolean).join(' '))
}

function membershipManagedRow(node) {
  const status = membershipManagedStatus(node)
  const reasons = Array.isArray(node.readiness?.reasons)
    ? node.readiness.reasons.filter(reason => typeof reason === 'string')
    : []
  return Object.freeze({
    id: node.stable_id,
    kind: 'managed',
    label: node.naming.display_name,
    secondary: `${node.identity.source} · ${node.identity.network_id}`,
    status,
    active: node.managed.active === true,
    ready: node.readiness.scheduler_ready === true,
    alive: node.readiness.alive === true,
    reasons: Object.freeze([...reasons]),
    freshness: formatFleetObservationAge(node.readiness.observation_age_ms),
    searchText: membershipSearchText([
      node.naming.display_name,
      node.naming.alias,
      node.naming.provider_name,
      node.stable_id,
      node.identity.source,
      node.identity.network_id,
      node.identity.device_id,
      node.managed.state,
      node.managed.projection_generation,
      node.managed.membership_generation,
      node.managed.binding_generation,
      status.label,
      ...node.operations
    ]),
    node
  })
}

function membershipObservedRow(node) {
  const observation = node.observation
  return Object.freeze({
    id: node.stable_id,
    kind: 'observed',
    label: node.naming.display_name,
    secondary: `${node.provider.label} · ${node.provider.network_id}`,
    status: { key: 'observed', label: 'Observed · unmanaged', tone: 'muted' },
    active: false,
    ready: false,
    alive: observation.online === true,
    reasons: Object.freeze([]),
    freshness: observation.last_observed_at || 'Observation time unavailable',
    searchText: membershipSearchText([
      node.naming.display_name,
      node.naming.technical_name,
      node.provider.label,
      node.provider.kind,
      node.provider.network_id,
      node.provider.instance_id,
      node.provider.node_id,
      observation.hostname,
      observation.observed_id,
      observation.classification,
      ...observation.addresses,
      ...observation.tags
    ]),
    node
  })
}

export function buildFleetMembershipCenterModel(overview) {
  if (!isPlainRecord(overview)) throw new Error('invalid Fleet Membership overview')
  const rows = buildFleetCanvasNodes(overview).map(node =>
    node.kind === 'observed'
      ? membershipObservedRow(node)
      : membershipManagedRow(node)
  )
  const managedRows = rows.filter(row => row.kind === 'managed')
  const disabled = managedRows.filter(row => row.node.managed.state === 'disabled').length
  const removed = managedRows.filter(row => row.node.managed.state === 'removed').length
  const active = managedRows.filter(row => row.active).length
  const ready = managedRows.filter(row => row.ready).length
  const attention = managedRows.filter(row => row.active && !row.ready).length
  const observed = rows.filter(row => row.kind === 'observed').length
  return Object.freeze({
    rows: Object.freeze(rows),
    summary: Object.freeze({
      visible: rows.length,
      managed: managedRows.length,
      active,
      notActive: disabled + removed,
      disabled,
      removed,
      ready,
      attention,
      observed
    }),
    observationStatus: providerObservationStatus(overview)
  })
}

function membershipFilterMatches(row, filter) {
  if (filter === 'all') return true
  if (filter === 'observed') return row.kind === 'observed'
  if (row.kind !== 'managed') return false
  if (filter === 'managed') return true
  if (filter === 'active') return row.active
  if (filter === 'not_active') return !row.active
  if (filter === 'disabled') return row.node.managed.state === 'disabled'
  if (filter === 'removed') return row.node.managed.state === 'removed'
  if (filter === 'ready') return row.ready
  if (filter === 'attention') return row.active && !row.ready
  return false
}

export function filterFleetMembershipRows(rows, query = '', filter = 'all') {
  const tokens = normalizedSearch(query).split(/\s+/).filter(Boolean)
  return rows.filter(row =>
    membershipFilterMatches(row, filter) &&
    tokens.every(token => row.searchText.includes(token))
  )
}

function membershipStage(key, label, state, tone, detail) {
  return Object.freeze({ key, label, state, tone, detail })
}

export function buildMembershipAuthorityStages(row) {
  if (!isPlainRecord(row) || !['managed', 'observed'].includes(row.kind)) {
    throw new Error('invalid Membership authority row')
  }
  if (row.kind === 'observed') {
    const node = row.node
    return Object.freeze([
      membershipStage(
        'provider',
        'Provider visibility',
        'evidence',
        'muted',
        `${node.provider.label} reports this machine on network ${node.provider.network_id}.`
      ),
      membershipStage(
        'projection',
        'Managed projection',
        'absent',
        'muted',
        'No Fleet-managed projection is associated with this provider observation.'
      ),
      membershipStage(
        'membership',
        'Membership generation',
        'unavailable',
        'muted',
        'No managed membership-generation evidence is available for an observed-only machine.'
      ),
      membershipStage(
        'binding',
        'Binding generation',
        'unavailable',
        'muted',
        'No managed binding-generation evidence is available for an observed-only machine.'
      ),
      membershipStage(
        'admission',
        'Fleet admission',
        'none',
        'muted',
        'Provider visibility does not grant Fleet admission or operation authority.'
      ),
      membershipStage(
        'readiness',
        'Operational readiness',
        'not applicable',
        'muted',
        'Scheduler readiness is evaluated only for managed Fleet nodes.'
      )
    ])
  }

  const node = row.node
  const admissionTone = node.managed.active
    ? 'good'
    : node.managed.state === 'removed' ? 'bad' : 'warn'
  const readinessTone = row.ready ? 'good' : row.active ? 'warn' : 'muted'
  const readinessState = row.ready
    ? 'ready'
    : row.active ? 'blocked' : 'not active'
  return Object.freeze([
    membershipStage(
      'provider',
      'Provider visibility',
      'not joined',
      'muted',
      'The Desktop contract does not join provider-observation rows to this managed identity.'
    ),
    membershipStage(
      'projection',
      'Managed projection',
      'accepted',
      'good',
      `Fleet accepted managed projection generation ${node.managed.projection_generation}.`
    ),
    membershipStage(
      'membership',
      'Membership generation',
      `generation ${node.managed.membership_generation}`,
      'muted',
      'This generation is accepted projection version evidence, not a live current trust check.'
    ),
    membershipStage(
      'binding',
      'Binding generation',
      `generation ${node.managed.binding_generation}`,
      'muted',
      'This generation is accepted projection version evidence, not proof of a live authenticated Keryx peer binding.'
    ),
    membershipStage(
      'admission',
      'Fleet admission',
      node.managed.state,
      admissionTone,
      node.managed.active
        ? 'This managed record is currently active in Fleet.'
        : `This managed record is currently ${node.managed.state}.`
    ),
    membershipStage(
      'readiness',
      'Operational readiness',
      readinessState,
      readinessTone,
      row.ready
        ? `Scheduler ready · ${row.freshness}.`
        : row.active
          ? `${row.reasons.length} readiness blocker${row.reasons.length === 1 ? '' : 's'} · ${row.freshness}.`
          : 'Inactive managed records are not scheduler ready.'
    )
  ])
}

function MembershipSummaryStrip({ summary, activeFilter, onFilter }) {
  const items = [
    ['all', 'Visible', summary.visible],
    ['managed', 'Managed', summary.managed],
    ['active', 'Active', summary.active],
    ['not_active', 'Not active', summary.notActive],
    ['ready', 'Ready', summary.ready],
    ['attention', 'Attention', summary.attention],
    ['observed', 'Observed', summary.observed]
  ]
  return jsx('section', {
    className: 'grid gap-2 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7',
    'aria-label': 'Fleet membership summary',
    children: items.map(([filter, label, value]) =>
      jsxs('button', {
        type: 'button',
        className: `rounded-xl border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${activeFilter === filter ? 'border-foreground/25 bg-muted/30' : 'border-border bg-muted/10 hover:bg-muted/20'}`,
        'aria-pressed': activeFilter === filter,
        onClick: () => onFilter(filter),
        children: [
          jsx('div', {
            className: 'text-[0.625rem] font-medium uppercase tracking-wide text-muted-foreground',
            children: label
          }),
          jsx('div', {
            className: 'mt-1 text-xl font-semibold tabular-nums text-foreground',
            children: value
          })
        ]
      }, filter)
    )
  })
}

function MembershipRowCard({ row, selected, onSelect }) {
  const managed = row.kind === 'managed'
  const node = row.node
  const badges = managed
    ? [
        { label: 'Managed', tone: 'neutral' },
        { label: node.managed.state, tone: node.managed.active ? 'good' : 'attention' },
        { label: row.status.label, tone: row.status.tone }
      ]
    : [
        { label: 'Observed', tone: 'info' },
        { label: 'Unmanaged', tone: 'neutral' },
        { label: node.provider.label, tone: 'neutral' }
      ]
  const evidence = managed
    ? `Projection ${node.managed.projection_generation} · membership ${node.managed.membership_generation} · binding ${node.managed.binding_generation}`
    : `${node.observation.addresses.length} address${node.observation.addresses.length === 1 ? '' : 'es'} · provider evidence only`
  return jsxs('button', {
    type: 'button',
    className: `w-full rounded-xl border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${selected ? 'border-foreground/25 bg-muted/30' : 'border-border bg-muted/10 hover:bg-muted/20'}`,
    'aria-pressed': selected,
    onClick: () => onSelect(row.id),
    children: [
      jsxs('div', {
        className: 'flex items-start gap-3',
        children: [
          jsx('span', { className: 'pt-1', children: jsx(StatusDot, { tone: row.status.tone }) }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: 'truncate text-sm font-medium text-foreground',
                children: row.label
              }),
              jsx('div', {
                className: 'mt-0.5 truncate text-[0.6875rem] text-muted-foreground',
                children: row.secondary
              })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'mt-2 flex flex-wrap gap-1.5',
        children: badges.map(badge =>
          jsx(NodeBadge, { badge }, `${badge.label}:${badge.tone}`)
        )
      }),
      jsx('div', {
        className: 'mt-2 text-[0.6875rem] leading-4 text-muted-foreground',
        children: evidence
      })
    ]
  })
}

function MembershipAuthorityLadder({ row }) {
  const stages = buildMembershipAuthorityStages(row)
  return jsx('ol', {
    className: 'grid gap-2',
    'aria-label': 'Membership authority ladder',
    children: stages.map(stage =>
      jsxs('li', {
        className: 'grid grid-cols-[auto_minmax(0,1fr)] gap-2 rounded-lg border border-border bg-muted/10 p-2.5',
        children: [
          jsx('span', { className: 'pt-1', children: jsx(StatusDot, { tone: stage.tone }) }),
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsxs('div', {
                className: 'flex flex-wrap items-baseline justify-between gap-2',
                children: [
                  jsx('strong', { className: 'text-xs text-foreground', children: stage.label }),
                  jsx('span', {
                    className: 'text-[0.625rem] font-medium uppercase tracking-wide text-muted-foreground',
                    children: stage.state
                  })
                ]
              }),
              jsx('p', {
                className: 'mt-1 text-[0.6875rem] leading-4 text-muted-foreground',
                children: stage.detail
              })
            ]
          })
        ]
      }, stage.key)
    )
  })
}

function ManagedMembershipDetail({ row }) {
  const node = row.node
  const resources = buildResourceRows(node.readiness)
  return jsxs('div', {
    className: 'grid gap-5 p-4 text-xs',
    children: [
      jsxs('div', {
        className: 'rounded-xl border border-border bg-muted/10 p-3',
        children: [
          jsx('div', {
            className: 'font-medium text-foreground',
            children: 'Current contract boundary'
          }),
          jsx('p', {
            className: 'mt-1 leading-5 text-muted-foreground',
            children: 'Fleet has accepted managed projection evidence, but the Desktop contract does not expose a live trusted flag or authenticated Keryx peer ID. Membership and binding generations are version evidence, not live trust or binding-health proof.'
          })
        ]
      }),
      jsx(InspectorSection, {
        title: 'Authority ladder',
        children: jsx(MembershipAuthorityLadder, { row })
      }),
      jsx(InspectorSection, {
        title: 'Projection evidence',
        children: jsxs('dl', {
          className: 'grid gap-2.5',
          children: [
            jsx(InspectorRow, { label: 'State', value: node.managed.state }),
            jsx(InspectorRow, {
              label: 'Projection', value: node.managed.projection_generation, mono: true
            }),
            jsx(InspectorRow, {
              label: 'Membership', value: node.managed.membership_generation, mono: true
            }),
            jsx(InspectorRow, {
              label: 'Binding', value: node.managed.binding_generation, mono: true
            })
          ]
        })
      }),
      jsx(InspectorSection, {
        title: 'Readiness and resources',
        children: jsxs('div', {
          className: 'grid gap-3',
          children: [
            row.reasons.length
              ? jsx('ul', {
                  className: 'grid gap-1 text-foreground',
                  children: row.reasons.map(reason =>
                    jsx('li', { children: describeReadinessReason(reason) }, reason)
                  )
                })
              : jsx('p', {
                  className: 'text-muted-foreground',
                  children: row.ready ? 'No readiness blockers.' : 'No readiness blocker detail reported.'
                }),
            jsx('dl', {
              className: 'grid gap-2',
              children: resources.map(item =>
                jsx(InspectorRow, { label: item.label, value: item.value }, item.key)
              )
            })
          ]
        })
      }),
      jsx(InspectorSection, {
        title: 'Advertised operations',
        children: node.operations.length
          ? jsx('ul', {
              className: 'flex flex-wrap gap-1.5',
              children: node.operations.map(operation =>
                jsx('li', {
                  className: 'rounded border border-border px-2 py-1 font-mono text-[0.6875rem] text-foreground',
                  children: operation
                }, operation)
              )
            })
          : jsx('p', {
              className: 'text-muted-foreground',
              children: 'No operations advertised.'
            })
      }),
      jsx(InspectorSection, {
        title: 'Exact identity',
        children: jsxs('dl', {
          className: 'grid gap-2.5',
          children: [
            jsx(InspectorRow, { label: 'Stable ID', value: node.stable_id, mono: true }),
            jsx(InspectorRow, { label: 'Source', value: node.identity.source, mono: true }),
            jsx(InspectorRow, { label: 'Network', value: node.identity.network_id, mono: true }),
            jsx(InspectorRow, { label: 'Device', value: node.identity.device_id, mono: true })
          ]
        })
      })
    ]
  })
}

function ObservedMembershipDetail({ row }) {
  const node = row.node
  const observation = node.observation
  return jsxs('div', {
    className: 'grid gap-5 p-4 text-xs',
    children: [
      jsx('p', {
        className: 'rounded-xl border border-border bg-muted/10 p-3 leading-5 text-muted-foreground',
        children: 'Provider observation only. Visibility is not trust, Keryx binding, Fleet admission, scheduler readiness, or execution authority.'
      }),
      jsx(InspectorSection, {
        title: 'Authority ladder',
        children: jsx(MembershipAuthorityLadder, { row })
      }),
      jsx(InspectorSection, {
        title: 'Provider evidence',
        children: jsxs('dl', {
          className: 'grid gap-2.5',
          children: [
            jsx(InspectorRow, { label: 'Provider', value: node.provider.label }),
            jsx(InspectorRow, { label: 'Network', value: node.provider.network_id, mono: true }),
            jsx(InspectorRow, {
              label: 'Online', value: observation.online == null ? 'Unknown' : observation.online ? 'Yes' : 'No'
            }),
            jsx(InspectorRow, {
              label: 'Addresses',
              value: observation.addresses.length ? observation.addresses.join(', ') : 'None observed',
              mono: true
            }),
            jsx(InspectorRow, {
              label: 'Tags',
              value: observation.tags.length ? observation.tags.join(', ') : 'None observed',
              mono: true
            }),
            jsx(InspectorRow, { label: 'Last observed', value: observation.last_observed_at })
          ]
        })
      }),
      jsx(InspectorSection, {
        title: 'Exact provider identity',
        children: jsxs('dl', {
          className: 'grid gap-2.5',
          children: [
            jsx(InspectorRow, { label: 'Provider node', value: node.provider.node_id, mono: true }),
            jsx(InspectorRow, { label: 'Instance', value: node.provider.instance_id, mono: true }),
            jsx(InspectorRow, { label: 'Observed ID', value: observation.observed_id, mono: true }),
            jsx(InspectorRow, { label: 'Classification', value: observation.classification })
          ]
        })
      })
    ]
  })
}

function MembershipDetail({ row }) {
  if (!row) {
    return jsxs('div', {
      className: 'grid h-full place-items-center p-6 text-center',
      children: jsx('div', {
        className: 'max-w-sm',
        children: [
          jsx(Codicon, { name: 'organization', size: '1.2rem' }),
          jsx('h2', {
            className: 'mt-3 text-sm font-semibold text-foreground',
            children: 'Select a machine'
          }),
          jsx('p', {
            className: 'mt-1 text-xs leading-5 text-muted-foreground',
            children: 'Inspect managed projection evidence, Fleet admission, readiness, or provider-only observation evidence.'
          })
        ]
      })
    })
  }
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('header', {
        className: 'flex items-start gap-3 border-b border-border px-4 py-3',
        children: [
          jsx('span', { className: 'pt-1', children: jsx(StatusDot, { tone: row.status.tone }) }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: 'truncate text-sm font-semibold text-foreground',
                children: row.label
              }),
              jsx('div', {
                className: 'mt-0.5 truncate text-[0.6875rem] text-muted-foreground',
                children: row.secondary
              })
            ]
          }),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'outline',
            onClick: () => openFleetNetwork({
              status: row.kind === 'observed' ? 'observed' : 'managed',
              selectedId: row.id
            }),
            children: 'Inspect in Network'
          })
        ]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: row.kind === 'observed'
          ? jsx(ObservedMembershipDetail, { row })
          : jsx(ManagedMembershipDetail, { row })
      })
    ]
  })
}

function FleetMembershipCenter({ overview, connection, refresh }) {
  const section = getFleetSection('members')
  const model = useMemo(() => buildFleetMembershipCenterModel(overview), [overview])
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [selectedId, setSelectedId] = useState(null)
  const visibleRows = useMemo(
    () => filterFleetMembershipRows(model.rows, query, filter),
    [filter, model.rows, query]
  )
  const selectedRow = model.rows.find(row => row.id === selectedId) ?? null

  useEffect(() => {
    if (selectedId && !visibleRows.some(row => row.id === selectedId)) {
      setSelectedId(null)
    }
  }, [selectedId, visibleRows])

  const headerMeta = `${model.summary.active}/${model.summary.managed} active · ${model.summary.ready} ready · ${model.summary.observed} observed`
  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col overflow-hidden',
    children: [
      jsx(FleetSectionHeader, {
        section,
        meta: headerMeta,
        actions: [
          jsx(ConnectionChip, { state: connection }, 'connection'),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'outline',
            onClick: refresh,
            children: 'Refresh'
          }, 'refresh'),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'outline',
            onClick: () => openFleetNetwork({ status: 'all' }),
            children: 'Open Network'
          }, 'network')
        ]
      }),
      jsxs('div', {
        className: 'grid min-h-0 flex-1 grid-rows-[auto_auto_minmax(0,1fr)] gap-3 p-4',
        children: [
          jsx(MembershipSummaryStrip, {
            summary: model.summary,
            activeFilter: filter,
            onFilter: setFilter
          }),
          jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              jsx(SearchField, {
                value: query,
                onChange: setQuery,
                placeholder: 'Search membership evidence',
                'aria-label': 'Search Fleet membership evidence',
                containerClassName: 'min-w-60 flex-1 sm:max-w-sm'
              }),
              ...MEMBERSHIP_FILTERS.map(([key, label]) =>
                jsx(Button, {
                  type: 'button',
                  size: 'sm',
                  variant: filter === key ? 'secondary' : 'ghost',
                  'aria-pressed': filter === key,
                  onClick: () => setFilter(key),
                  children: label
                }, key)
              ),
              jsx('span', {
                className: 'ml-auto text-[0.6875rem] text-muted-foreground',
                children: `${visibleRows.length}/${model.rows.length} shown · ${model.observationStatus}`
              })
            ]
          }),
          !model.rows.length
            ? jsxs('div', {
                className: 'grid min-h-0 place-items-center rounded-xl border border-border bg-muted/10 p-6 text-center',
                children: jsx('div', {
                  className: 'max-w-md',
                  children: [
                    jsx(Codicon, { name: 'organization', size: '1.25rem' }),
                    jsx('h2', {
                      className: 'mt-3 text-sm font-semibold text-foreground',
                      children: 'No membership evidence yet'
                    }),
                    jsx('p', {
                      className: 'mt-1 text-xs leading-5 text-muted-foreground',
                      children: 'Fleet currently has no managed projection rows or provider observations to display.'
                    })
                  ]
                })
              })
            : jsxs('div', {
                className: 'grid min-h-0 gap-3 lg:grid-cols-[minmax(19rem,0.8fr)_minmax(0,1.2fr)]',
                children: [
                  jsx('section', {
                    className: 'min-h-0 overflow-hidden rounded-xl border border-border bg-muted/5',
                    'aria-label': 'Fleet membership rows',
                    children: visibleRows.length
                      ? jsx(ScrollArea, {
                          className: 'h-full min-h-0',
                          children: jsx('div', {
                            className: 'grid gap-2 p-3',
                            children: visibleRows.map(row =>
                              jsx(MembershipRowCard, {
                                row,
                                selected: row.id === selectedId,
                                onSelect: setSelectedId
                              }, row.id)
                            )
                          })
                        })
                      : jsxs('div', {
                          className: 'grid h-full place-items-center p-6 text-center',
                          children: jsx('div', {
                            children: [
                              jsx(Codicon, { name: 'search-stop', size: '1.1rem' }),
                              jsx('p', {
                                className: 'mt-2 text-xs text-muted-foreground',
                                children: 'No membership evidence matches this view.'
                              })
                            ]
                          })
                        })
                  }),
                  jsx('section', {
                    className: 'min-h-0 overflow-hidden rounded-xl border border-border bg-background',
                    'aria-label': 'Membership evidence inspector',
                    children: jsx(MembershipDetail, { row: selectedRow })
                  })
                ]
              })
        ]
      })
    ]
  })
}
"""

replace_once(
    PLUGIN,
    "const FLEET_PLACEHOLDER_COPY = Object.freeze({",
    membership_block + "\nconst FLEET_PLACEHOLDER_COPY = Object.freeze({",
)

replace_once(
    PLUGIN,
    """  members: {
    title: 'Membership surface reserved',
    body: 'Managed membership inspection will connect through an authenticated Nodescale operator contract. Provider observations are not treated as trust or admission.'
  },
""",
    "",
)

replace_once(
    PLUGIN,
    """  Object.freeze({
    id: 'members', label: 'Members', icon: 'organization', path: '/fleet/members',
    detail: 'Membership controls await an authenticated operator contract.', availability: 'reserved'
  }),
""",
    """  Object.freeze({
    id: 'members', label: 'Members', icon: 'organization', path: '/fleet/members',
    detail: 'Inspect managed admission and projection generation evidence.', availability: 'available'
  }),
""",
)

replace_once(
    PLUGIN,
    """  if (sectionId === 'workflows') {
""",
    """  if (sectionId === 'members') {
    return jsx(FleetMembershipCenter, {
      overview,
      connection: events.connection,
      refresh: query.refetch
    })
  }

  if (sectionId === 'workflows') {
""",
)

replace_once(
    PLUGIN,
    "const operational = ['overview', 'network', 'workflows'].includes(section.id)",
    "const operational = ['overview', 'network', 'members', 'workflows'].includes(section.id)",
)

docs = DOCS.read_text(encoding="utf-8")
marker = "## Membership Center"
if marker in docs:
    raise RuntimeError("Membership Center docs already present")
docs += r"""

## Membership Center

`/fleet/members` is a read-only membership and admission surface over the same validated `fleet.desktop.v2` overview used by Overview and Network. It combines managed Fleet projection rows with separate provider observations while preserving their authority boundaries.

For managed rows, Membership shows Fleet admission state, projection generation, membership generation, binding generation, current readiness/freshness evidence, capacity/resources, and explicitly advertised operations. Membership and binding generation numbers are accepted projection-version evidence. They are not a live trust check and do not prove a currently healthy authenticated Keryx peer binding.

For observed rows, Membership shows only provider evidence. Visibility never implies trust, Keryx binding, Fleet admission, scheduler readiness, or execution authority. The current Desktop contract also does not join provider observations to managed identities, so Membership does not infer that relationship from names, addresses, tags, or network placement.

The exact live Nodescale trust/revocation and Keryx-binding control surface remains reserved for the authenticated Nodescale operator contract. Phase 3 adds no trust, membership, Keryx, scheduling, profile, or execution mutations.
"""
DOCS.write_text(docs, encoding="utf-8")

tests = TESTS.read_text(encoding="utf-8")
function_name = "test_phase3_membership_center_preserves_authority_boundaries"
if function_name in tests:
    raise RuntimeError("Phase 3 membership test already present")
tests += r'''


def test_phase3_membership_center_preserves_authority_boundaries() -> None:
    source = PLUGIN.read_text(encoding="utf-8")
    for contract in (
        "export const MEMBERSHIP_FILTERS",
        "export function buildFleetMembershipCenterModel",
        "export function filterFleetMembershipRows",
        "export function buildMembershipAuthorityStages",
        "function FleetMembershipCenter",
        "function MembershipAuthorityLadder",
        "function ManagedMembershipDetail",
        "function ObservedMembershipDetail",
        "'aria-label': 'Membership authority ladder'",
        "Membership and binding generations are version evidence",
        "does not expose a live trusted flag or authenticated Keryx peer ID",
        "sectionId === 'members'",
        "['overview', 'network', 'members', 'workflows']",
    ):
        assert contract in source
    assert "Membership surface reserved" not in source
    assert "Membership controls await an authenticated operator contract." not in source
    assert "id: 'members', label: 'Members'" in source
    assert "availability: 'available'" in source

    script = r"""
import fs from 'node:fs'
const dataUrl = source =>
  `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`
const sdkUrl = dataUrl(`
  export const ROUTES_AREA = 'app.routes'
  export const SIDEBAR_NAV_AREA = 'app.sidebar.nav'
  export const Button = 'Button'
  export const Codicon = 'Codicon'
  export const ContextMenu = 'ContextMenu'
  export const ContextMenuContent = 'ContextMenuContent'
  export const ContextMenuItem = 'ContextMenuItem'
  export const ContextMenuSeparator = 'ContextMenuSeparator'
  export const ContextMenuTrigger = 'ContextMenuTrigger'
  export const ScrollArea = 'ScrollArea'
  export const SearchField = 'SearchField'
  export const SegmentedControl = 'SegmentedControl'
  export const EmptyState = 'EmptyState'
  export const ErrorState = 'ErrorState'
  export const Loader = 'Loader'
  export const StatusDot = 'StatusDot'
  export const PALETTE_AREA = 'palette'
  export const STATUSBAR_AREAS = { right: 'status:right' }
  export const host = { navigate: () => undefined, notify: () => undefined }
  export const queryClient = {
    getQueryData: () => undefined,
    setQueryData: () => undefined,
    invalidateQueries: () => Promise.resolve()
  }
  export const useQuery = () => { throw new Error('render was not expected') }
`)
const reactUrl = dataUrl(`
  export const memo = value => value
  export const useCallback = value => value
  export const useEffect = () => undefined
  export const useMemo = factory => factory()
  export const useRef = value => ({ current: value })
  export const useState = value => [
    typeof value === 'function' ? value() : value,
    () => {}
  ]
`)
const jsxUrl = dataUrl(`
  export const jsx = (type, props, key) => ({ type, props, key })
  export const jsxs = jsx
`)
let source = fs.readFileSync(process.argv[1], 'utf8')
source = source.replaceAll("'@hermes/plugin-sdk'", `'${sdkUrl}'`)
source = source.replaceAll("'react/jsx-runtime'", `'${jsxUrl}'`)
source = source.replaceAll("'react'", `'${reactUrl}'`)
const mod = await import(dataUrl(source))

const managed = (id, options = {}) => ({
  stable_id: `fleet-node-${id.repeat(64).slice(0, 64)}`,
  identity: {
    source: 'nodescale',
    network_id: 'network-a',
    device_id: `device-${id}`
  },
  naming: {
    display_name: options.name ?? `compute-${id}`,
    provider_name: null,
    alias: null,
    has_alias: false
  },
  managed: {
    state: options.state ?? 'active',
    active: (options.state ?? 'active') === 'active',
    projection_generation: options.projection ?? '10',
    membership_generation: options.membership ?? '20',
    binding_generation: options.binding ?? '30'
  },
  readiness: {
    managed_state: options.state ?? 'active',
    alive: options.alive ?? true,
    scheduler_ready: options.ready ?? false,
    fresh: options.fresh ?? true,
    observation_age_ms: options.age ?? 12000,
    reasons: options.reasons ?? (options.ready ? [] : ['no_worker_capacity']),
    last_observation: options.hasObservation === false ? null : {
      network: 'reachable',
      keryx: 'available',
      hermes: 'available',
      worker: 'available'
    },
    capacity: options.capacity ?? {
      active_workers: 1,
      max_workers: 2,
      available_worker_slots: options.ready ? 1 : 0
    },
    resources: null,
    profiles: []
  },
  operations: ['fleet.health', 'fleet.inventory']
})

const observed = {
  observed_id: 'sha256:' + 'a'.repeat(64),
  network_id: 'network-a',
  provider_kind: 'headscale',
  provider_instance_id: 'provider-instance-a',
  provider_node_id: 'provider-node-a',
  hostname: 'compute-observed',
  given_name: 'compute-observed.example.invalid',
  addresses: ['192.0.2.10'],
  tags: ['tag:worker'],
  registered_at: null,
  last_seen_at: '2026-08-10T00:00:00+00:00',
  expires_at: null,
  online: true,
  expired: false,
  classification: 'discovered_unmanaged',
  first_observed_at: '2026-08-10T00:00:00+00:00',
  last_observed_at: '2026-08-10T00:00:10+00:00',
  snapshot_at: '2026-08-10T00:00:10+00:00'
}

const overview = {
  schema: 'fleet.desktop.v2',
  summary: {
    managed: 3,
    active: 2,
    alive: 2,
    ready: 1,
    not_ready: 1,
    observed_unmanaged: 1
  },
  nodes: [
    managed('a', { name: 'compute-a', ready: true, membership: '21', binding: '31' }),
    managed('b', { name: 'compute-b', ready: false, membership: '22', binding: '32' }),
    managed('c', {
      name: 'compute-c',
      state: 'disabled',
      alive: false,
      fresh: false,
      hasObservation: false,
      reasons: ['node_not_active'],
      capacity: null,
      membership: '23',
      binding: '33'
    })
  ],
  observed_nodes: [observed],
  observations: {
    state: 'available',
    network_id: 'network-a',
    reconciliation: {},
    truncated: false
  }
}
const model = mod.buildFleetMembershipCenterModel(overview)
const managedRow = model.rows.find(row => row.label === 'compute-a')
const observedRow = model.rows.find(row => row.kind === 'observed')
const stages = mod.buildMembershipAuthorityStages(managedRow)
const observedStages = mod.buildMembershipAuthorityStages(observedRow)
console.log(JSON.stringify({
  summary: model.summary,
  filters: mod.MEMBERSHIP_FILTERS,
  attention: mod.filterFleetMembershipRows(model.rows, '', 'attention').map(row => row.label),
  inactive: mod.filterFleetMembershipRows(model.rows, '', 'not_active').map(row => row.label),
  observed: mod.filterFleetMembershipRows(model.rows, '', 'observed').map(row => row.label),
  search: mod.filterFleetMembershipRows(model.rows, 'compute-b', 'all').map(row => row.label),
  managedStages: stages.map(stage => [stage.key, stage.state]),
  managedMembershipDetail: stages.find(stage => stage.key === 'membership').detail,
  managedBindingDetail: stages.find(stage => stage.key === 'binding').detail,
  observedStages: observedStages.map(stage => [stage.key, stage.state])
}))
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN)],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    loaded = json.loads(completed.stdout)
    assert loaded["summary"] == {
        "visible": 4,
        "managed": 3,
        "active": 2,
        "notActive": 1,
        "disabled": 1,
        "removed": 0,
        "ready": 1,
        "attention": 1,
        "observed": 1,
    }
    assert [item[0] for item in loaded["filters"]] == [
        "all",
        "managed",
        "active",
        "not_active",
        "disabled",
        "removed",
        "ready",
        "attention",
        "observed",
    ]
    assert loaded["attention"] == ["compute-b"]
    assert loaded["inactive"] == ["compute-c"]
    assert loaded["observed"] == ["compute-observed"]
    assert loaded["search"] == ["compute-b"]
    assert loaded["managedStages"] == [
        ["provider", "not joined"],
        ["projection", "accepted"],
        ["membership", "generation 21"],
        ["binding", "generation 31"],
        ["admission", "active"],
        ["readiness", "ready"],
    ]
    assert "not a live current trust check" in loaded["managedMembershipDetail"]
    assert "not proof of a live authenticated Keryx peer binding" in loaded["managedBindingDetail"]
    assert loaded["observedStages"] == [
        ["provider", "evidence"],
        ["projection", "absent"],
        ["membership", "unavailable"],
        ["binding", "unavailable"],
        ["admission", "none"],
        ["readiness", "not applicable"],
    ]
'''
TESTS.write_text(tests, encoding="utf-8")
