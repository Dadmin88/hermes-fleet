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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const QUERY_KEY = ['hermes-fleet', 'desktop', 'overview']
const LAYOUT_STORAGE_KEY = 'topology-layout.v1'
const NODE_WIDTH = 180
const NODE_HEIGHT = 92
const NODE_STEP_X = 240
const NODE_STEP_Y = 150
const MIN_SCALE = 0.5
const MAX_SCALE = 2.5
const POSITION_LIMIT = 100_000
const POSITION_LIMIT_COUNT = 256

function compareIds(left, right) {
  return left < right ? -1 : left > right ? 1 : 0
}

function finitePosition(value) {
  return Number.isFinite(value) && Math.abs(value) <= POSITION_LIMIT
}

export function sanitizeFleetPositions(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  const result = Object.create(null)
  const keys = Object.keys(value)
  if (keys.length > POSITION_LIMIT_COUNT) return result
  for (const id of keys.sort(compareIds)) {
    const position = value[id]
    if (
      typeof id === 'string' &&
      id.length > 0 &&
      id.length <= 128 &&
      position &&
      typeof position === 'object' &&
      !Array.isArray(position) &&
      finitePosition(position.x) &&
      finitePosition(position.y)
    ) {
      result[id] = { x: position.x, y: position.y }
    }
  }
  return result
}

function statusFor(node) {
  if (!node.managed.active) {
    return { key: 'inactive', label: 'INACTIVE', tone: 'muted' }
  }
  if (node.readiness.scheduler_ready) {
    return { key: 'ready', label: 'READY', tone: 'good' }
  }
  if (node.readiness.alive) {
    return { key: 'attention', label: 'NEEDS ATTENTION', tone: 'warn' }
  }
  return { key: 'awaiting', label: 'AWAITING EVIDENCE', tone: 'bad' }
}

function normalizedSearch(value) {
  return String(value ?? '')
    .normalize('NFKC')
    .trim()
    .toLowerCase()
}

export function buildFleetGraph(nodes, storedPositions = {}) {
  const positions = sanitizeFleetPositions(storedPositions)
  const idCounts = new Map()
  for (const node of nodes) {
    idCounts.set(node.stable_id, (idCounts.get(node.stable_id) ?? 0) + 1)
  }
  const ordered = nodes
    .filter(node => idCounts.get(node.stable_id) === 1)
    .sort((left, right) => compareIds(left.stable_id, right.stable_id))
  const columns = Math.max(1, Math.ceil(Math.sqrt(ordered.length)))
  const graphNodes = ordered.map((node, index) => {
    const saved = positions[node.stable_id]
    const status = statusFor(node)
    const x = saved?.x ?? (index % columns) * NODE_STEP_X
    const y = saved?.y ?? Math.floor(index / columns) * NODE_STEP_Y
    const searchText = normalizedSearch(
      [
        node.naming.display_name,
        node.naming.alias,
        node.naming.provider_name,
        node.identity.source,
        node.identity.network_id,
        node.identity.device_id,
        status.label,
        ...node.operations
      ]
        .filter(Boolean)
        .join(' ')
    )
    return {
      id: node.stable_id,
      label: node.naming.display_name,
      status,
      searchText,
      x,
      y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      source: node
    }
  })

  // fleet.desktop.v1 currently provides no relationship authority. The graph
  // engine supports edges, but the D2 adapter must remain edge-free until a
  // versioned Fleet relationship contract supplies real evidence.
  return { nodes: graphNodes, edges: [] }
}

export function filterFleetGraph(graph, query = '', statusFilter = 'all') {
  const tokens = normalizedSearch(query).split(/\s+/).filter(Boolean)
  const nodes = graph.nodes.filter(node => {
    const statusMatches = statusFilter === 'all' || node.status.key === statusFilter
    return statusMatches && tokens.every(token => node.searchText.includes(token))
  })
  const included = new Set(nodes.map(node => node.id))
  const edges = graph.edges.filter(
    edge => included.has(edge.source) && included.has(edge.target)
  )
  return { nodes, edges }
}

function clampScale(value) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value))
}

export function fitFleetGraph(nodes, viewportWidth, viewportHeight, padding = 64) {
  if (!nodes.length || viewportWidth <= 0 || viewportHeight <= 0) {
    return { x: 0, y: 0, scale: 1 }
  }
  const left = Math.min(...nodes.map(node => node.x))
  const top = Math.min(...nodes.map(node => node.y))
  const right = Math.max(...nodes.map(node => node.x + node.width))
  const bottom = Math.max(...nodes.map(node => node.y + node.height))
  const contentWidth = Math.max(1, right - left)
  const contentHeight = Math.max(1, bottom - top)
  const availableWidth = Math.max(1, viewportWidth - padding * 2)
  const availableHeight = Math.max(1, viewportHeight - padding * 2)
  const scale = clampScale(
    Math.min(availableWidth / contentWidth, availableHeight / contentHeight)
  )
  return {
    x: (viewportWidth - contentWidth * scale) / 2 - left * scale,
    y: (viewportHeight - contentHeight * scale) / 2 - top * scale,
    scale
  }
}

export function zoomFleetViewport(viewport, requestedScale, anchorX, anchorY) {
  const scale = clampScale(requestedScale)
  const worldX = (anchorX - viewport.x) / viewport.scale
  const worldY = (anchorY - viewport.y) / viewport.scale
  return {
    x: anchorX - worldX * scale,
    y: anchorY - worldY * scale,
    scale
  }
}

export function panFleetViewport(viewport, dx, dy) {
  return {
    ...viewport,
    x: viewport.x + dx,
    y: viewport.y + dy
  }
}

function setFleetPosition(positions, id, position) {
  const next = sanitizeFleetPositions(positions)
  if (!(id in next) && Object.keys(next).length >= POSITION_LIMIT_COUNT) {
    const evicted = Object.keys(next).sort(compareIds).at(-1)
    delete next[evicted]
  }
  next[id] = {
    x: Math.max(-POSITION_LIMIT, Math.min(POSITION_LIMIT, position.x)),
    y: Math.max(-POSITION_LIMIT, Math.min(POSITION_LIMIT, position.y))
  }
  return next
}

export function moveFleetPosition(positions, id, dx, dy) {
  const current = positions[id] ?? { x: 0, y: 0 }
  return setFleetPosition(positions, id, {
    x: current.x + dx,
    y: current.y + dy
  })
}

function shortLabel(value, limit = 24) {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`
}

function statusColor(status) {
  if (status.key === 'ready') return 'var(--ui-green)'
  if (status.key === 'attention') return 'var(--ui-yellow)'
  if (status.key === 'inactive') return 'var(--ui-text-quaternary)'
  return 'var(--ui-red)'
}

function SummaryItem({ label, value }) {
  return jsxs('div', {
    className: 'grid min-w-20 gap-1 border-l border-border pl-3 first:border-l-0 first:pl-0',
    children: [
      jsx('span', {
        className: 'text-[0.6875rem] uppercase tracking-wide text-muted-foreground',
        children: label
      }),
      jsx('strong', {
        className: 'text-lg font-semibold tabular-nums text-foreground',
        children: value
      })
    ]
  })
}

function GraphEdges({ edges, nodeById }) {
  return jsx('g', {
    'aria-hidden': true,
    children: edges.map(edge => {
      const source = nodeById.get(edge.source)
      const target = nodeById.get(edge.target)
      if (!source || !target) return null
      const sourceX = source.x + source.width
      const sourceY = source.y + source.height / 2
      const targetX = target.x
      const targetY = target.y + target.height / 2
      const bend = Math.max(40, Math.abs(targetX - sourceX) / 2)
      return jsx('path', {
        d: `M ${sourceX} ${sourceY} C ${sourceX + bend} ${sourceY}, ${targetX - bend} ${targetY}, ${targetX} ${targetY}`,
        fill: 'none',
        stroke: 'var(--ui-stroke-secondary)',
        strokeWidth: 2,
        vectorEffect: 'non-scaling-stroke'
      }, edge.id)
    })
  })
}

function GraphNode({
  node,
  selected,
  hovered,
  onSelect,
  onCenter,
  onHover,
  onMove,
  onPointerDown,
  onPointerMove,
  onPointerEnd
}) {
  const capacity = node.source.readiness.capacity
  const capacityLabel = capacity
    ? `Workers ${capacity.active_workers} / ${capacity.max_workers}`
    : 'No worker capacity'

  function focusSibling(event, offset, absolute = false) {
    const peers = [...event.currentTarget.parentElement.querySelectorAll('[data-fleet-node]')]
    const current = peers.indexOf(event.currentTarget)
    if (current < 0 || !peers.length) return
    const target = absolute
      ? peers[offset < 0 ? peers.length - 1 : 0]
      : peers[(current + offset + peers.length) % peers.length]
    onSelect(target.dataset.fleetNode)
    target.focus()
  }

  function onKeyDown(event) {
    const movement = {
      ArrowLeft: [-24, 0],
      ArrowRight: [24, 0],
      ArrowUp: [0, -24],
      ArrowDown: [0, 24]
    }[event.key]
    if (event.shiftKey && movement) {
      event.preventDefault()
      onMove(node.id, movement[0], movement[1])
    } else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault()
      focusSibling(event, 1)
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault()
      focusSibling(event, -1)
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      focusSibling(event, event.key === 'End' ? -1 : 1, true)
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect(node.id)
    }
  }

  return jsxs('g', {
    transform: `translate(${node.x} ${node.y})`,
    role: 'treeitem',
    tabIndex: selected ? 0 : -1,
    'aria-selected': selected,
    'aria-label': `${node.label}, ${node.status.label}, ${capacityLabel}, Stable identity ${node.id}`,
    'data-fleet-node': node.id,
    onClick: event => {
      event.stopPropagation()
      onSelect(node.id)
    },
    onDoubleClick: event => {
      event.stopPropagation()
      onCenter(node.id)
    },
    onMouseEnter: () => onHover(node.id),
    onMouseLeave: () => onHover(null),
    onKeyDown,
    onPointerDown: event => onPointerDown(event, node),
    onPointerMove,
    onPointerUp: onPointerEnd,
    onPointerCancel: onPointerEnd,
    onLostPointerCapture: onPointerEnd,
    style: { cursor: 'grab', outline: 'none' },
    children: [
      jsx('title', {
        children: `${node.label} — ${node.status.label} — Stable identity ${node.id}`
      }),
      jsx('rect', {
        width: node.width,
        height: node.height,
        rx: 12,
        fill: 'var(--ui-bg-editor)',
        stroke: selected
          ? 'var(--ui-accent)'
          : hovered
            ? 'var(--ui-text-primary)'
            : 'var(--ui-stroke-secondary)',
        strokeWidth: selected ? 3 : 1.5,
        vectorEffect: 'non-scaling-stroke'
      }),
      jsx('circle', {
        cx: 18,
        cy: 20,
        r: 5,
        fill: statusColor(node.status)
      }),
      jsx('text', {
        x: 31,
        y: 24,
        fill: 'var(--ui-text-primary)',
        fontSize: 13,
        fontWeight: 600,
        children: shortLabel(node.label)
      }),
      jsx('text', {
        x: 16,
        y: 52,
        fill: statusColor(node.status),
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.4,
        children: node.status.label
      }),
      jsx('text', {
        x: 16,
        y: 74,
        fill: 'var(--ui-text-tertiary)',
        fontSize: 11,
        children: capacityLabel
      })
    ]
  })
}

function FleetCanvas({ graph, positions, setPositions, commitPositions, selectedId, setSelectedId }) {
  const rootRef = useRef(null)
  const pointerRef = useRef(null)
  const initializedRef = useRef(false)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const [viewport, setViewport] = useState({ x: 0, y: 0, scale: 1 })
  const [hoveredId, setHoveredId] = useState(null)
  const nodeById = useMemo(
    () => new Map(graph.nodes.map(node => [node.id, node])),
    [graph.nodes]
  )

  useEffect(() => {
    const element = rootRef.current
    if (!element) return undefined
    const update = () => {
      const bounds = element.getBoundingClientRect()
      setSize({ width: bounds.width, height: bounds.height })
    }
    update()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(update)
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!initializedRef.current && size.width > 0 && size.height > 0 && graph.nodes.length) {
      initializedRef.current = true
      setViewport(fitFleetGraph(graph.nodes, size.width, size.height))
    }
  }, [graph.nodes, size])

  const fitAll = useCallback(() => {
    setViewport(fitFleetGraph(graph.nodes, size.width, size.height))
  }, [graph.nodes, size])

  const centerNode = useCallback(
    id => {
      const node = nodeById.get(id)
      if (!node || !size.width || !size.height) return
      const scale = Math.max(1, viewport.scale)
      setViewport({
        x: size.width / 2 - (node.x + node.width / 2) * scale,
        y: size.height / 2 - (node.y + node.height / 2) * scale,
        scale
      })
      setSelectedId(id)
    },
    [nodeById, setSelectedId, size, viewport.scale]
  )

  const moveNodeByKeyboard = useCallback(
    (id, dx, dy) => {
      setPositions(current => {
        const next = moveFleetPosition(current, id, dx, dy)
        commitPositions(next)
        return next
      })
    },
    [commitPositions, setPositions]
  )

  function beginPan(event) {
    if (event.button !== 0) return
    event.currentTarget.setPointerCapture(event.pointerId)
    pointerRef.current = {
      kind: 'pan',
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      viewport
    }
  }

  function beginNodeDrag(event, node) {
    if (event.button !== 0) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    setSelectedId(node.id)
    pointerRef.current = {
      kind: 'node',
      pointerId: event.pointerId,
      id: node.id,
      clientX: event.clientX,
      clientY: event.clientY,
      x: node.x,
      y: node.y
    }
  }

  function movePointer(event) {
    const active = pointerRef.current
    if (!active || active.pointerId !== event.pointerId) return
    const dx = event.clientX - active.clientX
    const dy = event.clientY - active.clientY
    if (active.kind === 'pan') {
      setViewport({
        ...active.viewport,
        x: active.viewport.x + dx,
        y: active.viewport.y + dy
      })
    } else {
      const next = setFleetPosition(positions, active.id, {
        x: active.x + dx / viewport.scale,
        y: active.y + dy / viewport.scale
      })
      active.positions = next
      setPositions(next)
    }
  }

  function endPointer(event) {
    const active = pointerRef.current
    if (!active || active.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    pointerRef.current = null
    if (active.kind === 'node') commitPositions(active.positions)
  }

  function onWheel(event) {
    event.preventDefault()
    const bounds = event.currentTarget.getBoundingClientRect()
    const anchorX = event.clientX - bounds.left
    const anchorY = event.clientY - bounds.top
    const factor = Math.exp(-event.deltaY * 0.0015)
    setViewport(current =>
      zoomFleetViewport(current, current.scale * factor, anchorX, anchorY)
    )
  }

  function onCanvasKeyDown(event) {
    if (event.target !== event.currentTarget) return
    const movement = {
      ArrowLeft: [40, 0],
      ArrowRight: [-40, 0],
      ArrowUp: [0, 40],
      ArrowDown: [0, -40]
    }[event.key]
    if (movement) {
      event.preventDefault()
      setViewport(current => panFleetViewport(current, movement[0], movement[1]))
    } else if (event.key === '+' || event.key === '=') {
      event.preventDefault()
      setViewport(current =>
        zoomFleetViewport(current, current.scale * 1.2, size.width / 2, size.height / 2)
      )
    } else if (event.key === '-') {
      event.preventDefault()
      setViewport(current =>
        zoomFleetViewport(current, current.scale / 1.2, size.width / 2, size.height / 2)
      )
    } else if (event.key === '0') {
      event.preventDefault()
      fitAll()
    }
  }

  return jsxs('div', {
    className: 'relative min-h-0 flex-1 overflow-hidden rounded-lg border border-border bg-muted/20',
    children: [
      jsxs('div', {
        className: 'absolute right-3 top-3 z-10 flex gap-2',
        children: [
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'outline',
            onClick: fitAll,
            disabled: !graph.nodes.length,
            children: 'Fit all'
          }),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'outline',
            onClick: () => centerNode(selectedId),
            disabled: !selectedId || !nodeById.has(selectedId),
            children: 'Center selected'
          })
        ]
      }),
      jsx('svg', {
        ref: rootRef,
        className: 'h-full min-h-80 w-full select-none',
        role: 'tree',
        tabIndex: 0,
        'aria-label': 'Fleet topology canvas. Arrow keys pan the canvas or move between focused nodes. Shift plus arrow moves a node. Plus and minus zoom; zero fits all.',
        style: { touchAction: 'none' },
        onPointerDown: beginPan,
        onPointerMove: movePointer,
        onPointerUp: endPointer,
        onPointerCancel: endPointer,
        onLostPointerCapture: endPointer,
        onWheel,
        onKeyDown: onCanvasKeyDown,
        children: jsx('g', {
          transform: `translate(${viewport.x} ${viewport.y}) scale(${viewport.scale})`,
          children: [
            jsx(GraphEdges, { edges: graph.edges, nodeById }),
            jsx('g', {
              children: graph.nodes.map(node =>
                jsx(GraphNode, {
                  node,
                  selected: selectedId === node.id,
                  hovered: hoveredId === node.id,
                  onSelect: setSelectedId,
                  onCenter: centerNode,
                  onHover: setHoveredId,
                  onMove: moveNodeByKeyboard,
                  onPointerDown: beginNodeDrag,
                  onPointerMove: movePointer,
                  onPointerEnd: endPointer
                }, node.id)
              )
            })
          ]
        })
      }),
      !graph.nodes.length
        ? jsx('div', {
            className: 'pointer-events-none absolute inset-0 grid place-items-center text-sm text-muted-foreground',
            children: 'No nodes match the current search and filter.'
          })
        : null
    ]
  })
}

const FILTERS = [
  ['all', 'All'],
  ['ready', 'Ready'],
  ['attention', 'Attention'],
  ['awaiting', 'Awaiting evidence'],
  ['inactive', 'Inactive']
]

function FleetCanvasWorkspace({ overview, ctx }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [positions, setPositions] = useState(() =>
    sanitizeFleetPositions(ctx.storage.get(LAYOUT_STORAGE_KEY, {}))
  )
  const [selectedId, setSelectedId] = useState(overview.nodes[0]?.stable_id ?? null)
  const positionsRef = useRef(positions)
  positionsRef.current = positions

  const graph = useMemo(
    () => buildFleetGraph(overview.nodes, positions),
    [overview.nodes, positions]
  )
  const visibleGraph = useMemo(
    () => filterFleetGraph(graph, query, filter),
    [filter, graph, query]
  )
  const commitPositions = useCallback(value => {
    const next = sanitizeFleetPositions(value ?? positionsRef.current)
    positionsRef.current = next
    ctx.storage.set(LAYOUT_STORAGE_KEY, next)
  }, [ctx.storage])

  useEffect(() => {
    if (selectedId && !graph.nodes.some(node => node.id === selectedId)) {
      setSelectedId(graph.nodes[0]?.id ?? null)
    } else if (
      visibleGraph.nodes.length &&
      !visibleGraph.nodes.some(node => node.id === selectedId)
    ) {
      setSelectedId(visibleGraph.nodes[0].id)
    }
  }, [graph.nodes, selectedId, visibleGraph.nodes])

  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('input', {
            type: 'search',
            value: query,
            onChange: event => setQuery(event.target.value),
            placeholder: 'Search Fleet nodes',
            'aria-label': 'Search Fleet nodes',
            className: 'h-9 min-w-56 rounded-md border border-input bg-background px-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring'
          }),
          ...FILTERS.map(([key, label]) =>
            jsx(Button, {
              type: 'button',
              size: 'sm',
              variant: filter === key ? 'secondary' : 'outline',
              'aria-pressed': filter === key,
              onClick: () => setFilter(key),
              children: label
            }, key)
          )
        ]
      }),
      jsx(FleetCanvas, {
        graph: visibleGraph,
        positions,
        setPositions,
        commitPositions,
        selectedId,
        setSelectedId
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground',
        children: [
          ...[
            ['good', 'Ready'],
            ['warn', 'Needs attention'],
            ['bad', 'Awaiting evidence'],
            ['muted', 'Inactive']
          ].map(([tone, label]) =>
            jsxs('span', {
              className: 'inline-flex items-center gap-1.5',
              children: [jsx(StatusDot, { tone }), label]
            }, label)
          ),
          jsx('span', {
            children: 'Drag nodes or use Shift+arrow to save local layout. Pan the background or focused canvas with arrows; wheel or +/- zoom; 0 fits all.'
          }),
          jsx('span', {
            children: 'Edges are hidden because Fleet does not yet expose relationship evidence.'
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
              jsx('h1', { className: 'text-base font-semibold text-foreground', children: 'Fleet Canvas' }),
              jsx('p', {
                className: 'mt-1 text-xs text-muted-foreground',
                children: 'Current managed-node readiness on a stable operator layout.'
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
      jsx(FleetCanvasWorkspace, { overview, ctx })
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
