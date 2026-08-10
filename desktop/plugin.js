import {
  Button,
  EmptyState,
  ErrorState,
  Loader,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  StatusDot,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

const QUERY_KEY = ['hermes-fleet', 'desktop', 'overview']

function statusFor(node) {
  if (!node.managed.active) {
    return { label: 'INACTIVE', tone: 'muted' }
  }
  if (node.readiness.scheduler_ready) {
    return { label: 'READY', tone: 'good' }
  }
  if (node.readiness.alive) {
    return { label: 'NEEDS ATTENTION', tone: 'warn' }
  }
  return { label: 'AWAITING EVIDENCE', tone: 'bad' }
}

function SummaryItem({ label, value }) {
  return jsxs('div', {
    className: 'grid min-w-20 gap-1 border-l border-border pl-3 first:border-l-0 first:pl-0',
    children: [
      jsx('span', { className: 'text-[0.6875rem] uppercase tracking-wide text-muted-foreground', children: label }),
      jsx('strong', { className: 'text-lg font-semibold tabular-nums text-foreground', children: value })
    ]
  })
}

function NodeRow({ node }) {
  const status = statusFor(node)
  const capacity = node.readiness.capacity
  const capacityLabel = capacity
    ? `${capacity.active_workers} / ${capacity.max_workers} active`
    : 'No worker capacity reported'

  return jsxs('article', {
    className: 'grid gap-3 rounded-lg border border-border bg-card px-4 py-3 shadow-sm',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'flex min-w-0 items-center gap-2',
            children: [
              jsx(StatusDot, { tone: status.tone }),
              jsx('h2', {
                className: 'truncate text-sm font-semibold text-foreground',
                title: node.naming.display_name,
                children: node.naming.display_name
              })
            ]
          }),
          jsx('span', {
            className: 'shrink-0 text-[0.6875rem] font-medium tracking-wide text-muted-foreground',
            children: status.label
          })
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground',
        children: [
          jsx('span', { children: capacityLabel }),
          jsx('span', {
            className: 'font-mono',
            title: node.stable_id,
            children: node.stable_id.slice(0, 19)
          })
        ]
      })
    ]
  })
}

function FleetPage({ ctx }) {
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => ctx.rest('/overview'),
    refetchInterval: 15_000,
    retry: 1
  })

  if (query.isPending) {
    return jsx('main', {
      className: 'grid h-full min-h-64 place-items-center',
      children: jsxs('div', {
        className: 'grid justify-items-center gap-3 text-center',
        children: [
          jsx(Loader, { label: 'Discovering Fleet nodes', type: 'lemniscate-bloom' }),
          jsx('div', { className: 'text-sm text-muted-foreground', children: 'Discovering nodes…' })
        ]
      })
    })
  }

  if (query.isError) {
    return jsx('main', {
      className: 'grid h-full min-h-64 place-items-center p-6',
      children: jsx(ErrorState, {
        title: 'Fleet is unavailable',
        description: 'Unable to reach the Fleet backend.',
        children: jsx(Button, {
          type: 'button',
          variant: 'outline',
          onClick: () => query.refetch(),
          children: 'Retry'
        })
      })
    })
  }

  const overview = query.data
  if (!overview.nodes.length) {
    return jsx('main', {
      className: 'grid h-full min-h-64 place-items-center p-6',
      children: jsx(EmptyState, {
        title: 'Your Fleet is empty',
        description: 'Managed nodes will appear here as they join Fleet.'
      })
    })
  }

  return jsxs('main', {
    className: 'flex h-full min-h-0 flex-col overflow-hidden bg-background',
    children: [
      jsxs('header', {
        className: 'flex flex-wrap items-start justify-between gap-4 border-b border-border px-5 py-4',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-base font-semibold text-foreground', children: 'Fleet' }),
              jsx('p', {
                className: 'mt-1 text-xs text-muted-foreground',
                children: 'Current managed-node readiness from Fleet.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex items-start gap-4',
            children: [
              jsx(SummaryItem, { label: 'Managed', value: overview.summary.managed }),
              jsx(SummaryItem, { label: 'Alive', value: overview.summary.alive }),
              jsx(SummaryItem, { label: 'Ready', value: overview.summary.ready }),
              jsx(SummaryItem, { label: 'Needs attention', value: overview.summary.not_ready })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto p-5',
        children: jsx('div', {
          className: 'mx-auto grid w-full max-w-4xl gap-2',
          children: overview.nodes.map(node => jsx(NodeRow, { node }, node.stable_id))
        })
      })
    ]
  })
}

export default {
  id: 'hermes-fleet',
  name: 'Fleet',
  description: 'Visual control plane for managed Hermes Fleet nodes.',
  register(ctx) {
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: '/fleet' },
      render: () => jsx(FleetPage, { ctx })
    })
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 55,
      data: { codicon: 'server-process', label: 'Fleet', path: '/fleet' }
    })
  }
}
