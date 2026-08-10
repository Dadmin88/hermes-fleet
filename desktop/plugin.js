import {
  Button,
  EmptyState,
  ErrorState,
  Loader,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  host,
  queryClient,
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
const EVENT_SCHEMA = 'fleet.desktop-events.v1'

export function diffFleetOverview(previous, current, sequence = 0) {
  const before = new Map((previous?.nodes ?? []).map(node => [node.stable_id, node]))
  const after = new Map((current?.nodes ?? []).map(node => [node.stable_id, node]))
  const entries = []
  for (const [id, node] of after) {
    const old = before.get(id)
    if (!old) {
      entries.push({ id: `${sequence}:${id}:added`, node_id: id, kind: 'added', message: `${node.naming.display_name} joined the managed view.` })
      continue
    }
    const oldStatus = statusFor(old).key
    const nextStatus = statusFor(node).key
    if (oldStatus !== nextStatus) {
      entries.push({
        id: `${sequence}:${id}:status`,
        node_id: id,
        kind: nextStatus === 'ready' ? 'recovered' : 'status',
        message: `${node.naming.display_name} changed from ${oldStatus} to ${nextStatus}.`
      })
    } else if (old.naming.display_name !== node.naming.display_name) {
      entries.push({ id: `${sequence}:${id}:renamed`, node_id: id, kind: 'renamed', message: `${old.naming.display_name} is now ${node.naming.display_name}.` })
    }
  }
  for (const [id, node] of before) {
    if (!after.has(id)) {
      entries.push({ id: `${sequence}:${id}:removed`, node_id: id, kind: 'removed', message: `${node.naming.display_name} left the managed view.` })
    }
  }
  return entries.slice(0, 64)
}

function validFleetEvent(value) {
  return Boolean(
    value &&
    value.schema === EVENT_SCHEMA &&
    Number.isSafeInteger(value.sequence) &&
    value.sequence > 0 &&
    ['snapshot', 'overview_changed', 'unavailable', 'recovered', 'heartbeat'].includes(value.kind)
  )
}

function useFleetEvents(ctx) {
  const [connection, setConnection] = useState('polling')
  const [activity, setActivity] = useState([])
  useEffect(() =>
    ctx.socket('/events', event => {
      if (!validFleetEvent(event)) return
      if (event.kind === 'unavailable') {
        setConnection('reconnecting')
        return
      }
      setConnection('live')
      if (event.kind === 'heartbeat') return
      if (!event.overview || event.overview.schema !== 'fleet.desktop.v1') return
      const previous = queryClient.getQueryData(QUERY_KEY)
      const changes = diffFleetOverview(previous, event.overview, event.sequence)
      if (changes.length) {
        setActivity(items => [...changes, ...items].slice(0, 64))
      }
      queryClient.setQueryData(QUERY_KEY, event.overview)
    }),
  [ctx])
  return { connection, activity, clearActivity: () => setActivity([]) }
}

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

const READINESS_REASON_DESCRIPTIONS = {
  node_unknown: 'Fleet does not know this managed identity.',
  node_not_active: 'The managed node is not active.',
  observation_missing: 'No readiness evidence has been received.',
  observation_stale: 'The latest readiness evidence is stale.',
  observation_time_invalid: 'The readiness evidence timestamp is invalid.',
  network_unreachable: 'The node network is unreachable.',
  keryx_unavailable: 'Keryx is unavailable.',
  hermes_unavailable: 'Hermes is unavailable.',
  worker_unavailable: 'The worker runtime is unavailable.',
  no_worker_capacity: 'No worker capacity is currently available.'
}

export function describeReadinessReason(reason) {
  return READINESS_REASON_DESCRIPTIONS[reason] ?? `Unknown readiness reason: ${reason}`
}

export function formatFleetAge(milliseconds) {
  if (!Number.isFinite(milliseconds) || milliseconds < 0) return 'No evidence'
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms ago`
  const seconds = milliseconds / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s ago`
  const minutes = seconds / 60
  if (minutes < 60) return `${minutes.toFixed(1)}m ago`
  const hours = minutes / 60
  if (hours < 24) return `${hours.toFixed(1)}h ago`
  return `${(hours / 24).toFixed(1)}d ago`
}

export function formatFleetBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return 'No evidence'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB', 'PiB']
  let value = bytes
  let unit = -1
  do {
    value /= 1024
    unit += 1
  } while (value >= 1024 && unit < units.length - 1)
  return `${value.toFixed(1)} ${units[unit]}`
}

function readinessStep(key, label, state, detail) {
  return { key, label, state, detail }
}

export function buildReadinessLadder(node) {
  const readiness = node.readiness
  const observation = readiness.last_observation
  const capacity = readiness.capacity
  return [
    readinessStep(
      'managed',
      'Managed',
      node.managed.active ? 'ready' : 'blocked',
      node.managed.active ? 'Active managed admission' : `Managed state: ${node.managed.state}`
    ),
    readinessStep(
      'fresh',
      'Fresh evidence',
      !observation ? 'unknown' : readiness.fresh ? 'ready' : 'blocked',
      !observation ? 'No observation' : formatFleetAge(readiness.observation_age_ms)
    ),
    readinessStep(
      'network',
      'Network',
      !observation ? 'unknown' : observation.network === 'reachable' ? 'ready' : 'blocked',
      observation?.network ?? 'No evidence'
    ),
    readinessStep(
      'keryx',
      'Keryx',
      !observation ? 'unknown' : observation.keryx === 'available' ? 'ready' : 'blocked',
      observation?.keryx ?? 'No evidence'
    ),
    readinessStep(
      'hermes',
      'Hermes',
      !observation ? 'unknown' : observation.hermes === 'available' ? 'ready' : 'blocked',
      observation?.hermes ?? 'No evidence'
    ),
    readinessStep(
      'worker',
      'Worker',
      !observation ? 'unknown' : observation.worker === 'available' ? 'ready' : 'blocked',
      observation?.worker ?? 'No evidence'
    ),
    readinessStep(
      'capacity',
      'Capacity',
      !capacity ? 'unknown' : capacity.available_worker_slots > 0 ? 'ready' : 'blocked',
      capacity ? `${capacity.available_worker_slots} worker slot(s) free` : 'No evidence'
    )
  ]
}

function byteCapacity(value) {
  return value
    ? `${formatFleetBytes(value.available_bytes)} free / ${formatFleetBytes(value.total_bytes)}`
    : 'No evidence'
}

export function buildResourceRows(readiness) {
  const capacity = readiness.capacity
  const resources = readiness.resources
  const rows = [
    {
      key: 'workers',
      label: 'Workers',
      value: capacity
        ? `${capacity.active_workers} / ${capacity.max_workers} active · ${capacity.available_worker_slots} free`
        : 'No evidence'
    },
    {
      key: 'cpu',
      label: 'CPU',
      value: resources?.cpu
        ? `${resources.cpu.logical_cores} logical · ${resources.cpu.load_basis_points == null ? 'load unavailable' : `${(resources.cpu.load_basis_points / 100).toFixed(2)}% load`}`
        : 'No evidence'
    },
    { key: 'ram', label: 'RAM', value: byteCapacity(resources?.ram) },
    { key: 'swap', label: 'Swap', value: byteCapacity(resources?.swap) },
    { key: 'disk', label: 'Disk', value: byteCapacity(resources?.disk) },
    {
      key: 'gpu',
      label: 'GPU',
      value: resources?.gpu ? (resources.gpu.present ? 'Present' : 'Not present') : 'No evidence'
    }
  ]
  if (resources?.gpu?.vram) {
    rows.push({ key: 'vram', label: 'VRAM', value: byteCapacity(resources.gpu.vram) })
  }
  if (!readiness.fresh) {
    const age = formatFleetAge(readiness.observation_age_ms)
    return rows.map(row =>
      row.value === 'No evidence'
        ? row
        : { ...row, value: `Last observed ${age}: ${row.value}` }
    )
  }
  return rows
}

export function aliasMutationBody(node, alias) {
  return {
    source: node.identity.source,
    network_id: node.identity.network_id,
    device_id: node.identity.device_id,
    binding_generation: node.managed.binding_generation,
    alias
  }
}

export function aliasClearMutationBody(node) {
  return {
    source: node.identity.source,
    network_id: node.identity.network_id,
    device_id: node.identity.device_id,
    binding_generation: node.managed.binding_generation
  }
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
  animated,
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
    className: animated ? 'motion-safe:animate-pulse transition-opacity duration-300' : 'transition-opacity duration-300',
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

function FleetCanvas({ graph, positions, setPositions, commitPositions, selectedId, setSelectedId, animatedIds }) {
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
                  animated: animatedIds.has(node.id),
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

function InspectorSection({ title, children }) {
  return jsxs('section', {
    className: 'grid gap-2 border-t border-border pt-3 first:border-t-0 first:pt-0',
    children: [
      jsx('h3', {
        className: 'text-xs font-semibold uppercase tracking-wide text-muted-foreground',
        children: title
      }),
      children
    ]
  })
}

function InspectorRow({ label, value, mono = false }) {
  return jsxs('div', {
    className: 'grid grid-cols-[minmax(5rem,auto)_minmax(0,1fr)] gap-3 text-xs',
    children: [
      jsx('dt', { className: 'text-muted-foreground', children: label }),
      jsx('dd', {
        className: `${mono ? 'font-mono ' : ''}min-w-0 break-words text-right text-foreground`,
        children: value
      })
    ]
  })
}

function ReadinessLadder({ node }) {
  const steps = buildReadinessLadder(node)
  return jsx('ol', {
    className: 'grid gap-2',
    'aria-label': 'Readiness ladder',
    children: steps.map(step =>
      jsxs('li', {
        className: 'grid grid-cols-[auto_minmax(0,1fr)] gap-2 rounded-md border border-border p-2',
        children: [
          jsx(StatusDot, {
            tone: step.state === 'ready' ? 'good' : step.state === 'blocked' ? 'bad' : 'muted'
          }),
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsxs('div', {
                className: 'flex items-center justify-between gap-2 text-xs',
                children: [
                  jsx('strong', { className: 'text-foreground', children: step.label }),
                  jsx('span', {
                    className: 'uppercase tracking-wide text-muted-foreground',
                    children: step.state
                  })
                ]
              }),
              jsx('p', {
                className: 'mt-1 break-words text-[0.6875rem] text-muted-foreground',
                children: step.detail
              })
            ]
          })
        ]
      }, step.key)
    )
  })
}

function NodeInspector({ node, ctx, refresh }) {
  const [alias, setAlias] = useState(node.naming.alias ?? '')
  const [mutation, setMutation] = useState({ state: 'idle', message: '' })
  const [copyMessage, setCopyMessage] = useState('')
  const pending = mutation.state === 'pending'
  const aliasValid = alias.length > 0 && alias.length <= 128 && alias.trim() === alias

  useEffect(() => {
    setAlias(node.naming.alias ?? '')
    setMutation({ state: 'idle', message: '' })
    setCopyMessage('')
  }, [node.naming.alias, node.stable_id])

  async function reconcile() {
    await refresh()
  }

  async function saveAlias() {
    if (!aliasValid || pending) return
    setMutation({ state: 'pending', message: 'Saving alias…' })
    try {
      await ctx.rest(`/nodes/${encodeURIComponent(node.stable_id)}/alias`, {
        method: 'PUT',
        body: aliasMutationBody(node, alias)
      })
      await reconcile()
      setMutation({ state: 'success', message: 'Alias saved.' })
    } catch {
      try {
        await reconcile()
      } catch {}
      setMutation({ state: 'error', message: 'Alias update was rejected.' })
    }
  }

  async function resetAlias() {
    if (!node.naming.has_alias || pending) return
    setMutation({ state: 'pending', message: 'Resetting name…' })
    try {
      await ctx.rest(`/nodes/${encodeURIComponent(node.stable_id)}/alias`, {
        method: 'DELETE',
        body: aliasClearMutationBody(node)
      })
      await reconcile()
      setMutation({ state: 'success', message: 'Provider name restored.' })
    } catch {
      try {
        await reconcile()
      } catch {}
      setMutation({ state: 'error', message: 'Name reset was rejected.' })
    }
  }

  async function copyStableIdentity() {
    try {
      if (!globalThis.navigator?.clipboard?.writeText) throw new Error('clipboard unavailable')
      await globalThis.navigator.clipboard.writeText(node.stable_id)
      setCopyMessage('Copied stable identity.')
    } catch {
      setCopyMessage('Unable to copy stable identity.')
    }
  }

  const reasons = node.readiness.reasons
  const resources = buildResourceRows(node.readiness)
  const providerFallback = node.naming.provider_name ?? node.identity.device_id

  return jsxs('aside', {
    className: 'min-h-0 w-full shrink-0 overflow-auto rounded-lg border border-border bg-background p-4 lg:w-96',
    'aria-label': `Inspector for ${node.naming.display_name}`,
    children: [
      jsxs('div', {
        className: 'mb-4 flex items-start justify-between gap-3',
        children: [
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('p', { className: 'text-xs text-muted-foreground', children: 'Node Inspector' }),
              jsx('h2', {
                className: 'truncate text-base font-semibold text-foreground',
                children: node.naming.display_name
              })
            ]
          }),
          jsx(StatusDot, { tone: statusFor(node).tone })
        ]
      }),
      jsxs('div', {
        className: 'grid gap-4',
        children: [
          jsx(InspectorSection, {
            title: 'Identity',
            children: jsxs('dl', {
              className: 'grid gap-2',
              children: [
                jsx(InspectorRow, { label: 'Source', value: node.identity.source, mono: true }),
                jsx(InspectorRow, { label: 'Network', value: node.identity.network_id, mono: true }),
                jsx(InspectorRow, { label: 'Device', value: node.identity.device_id, mono: true }),
                jsx(InspectorRow, { label: 'Stable ID', value: node.stable_id, mono: true }),
                jsx(InspectorRow, { label: 'Binding', value: node.managed.binding_generation, mono: true }),
                jsx(Button, {
                  type: 'button',
                  size: 'sm',
                  variant: 'outline',
                  onClick: copyStableIdentity,
                  children: 'Copy stable identity'
                }),
                jsx('p', {
                  className: 'text-[0.6875rem] text-muted-foreground',
                  'aria-live': 'polite',
                  children: copyMessage
                })
              ]
            })
          }),
          jsx(InspectorSection, {
            title: 'Name',
            children: jsxs('div', {
              className: 'grid gap-2',
              children: [
                jsx('label', {
                  className: 'text-xs text-muted-foreground',
                  htmlFor: `fleet-alias-${node.stable_id}`,
                  children: 'Operator alias'
                }),
                jsx('input', {
                  id: `fleet-alias-${node.stable_id}`,
                  value: alias,
                  maxLength: 128,
                  disabled: pending,
                  onChange: event => setAlias(event.target.value),
                  className: 'h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring'
                }),
                jsx('p', {
                  className: 'text-[0.6875rem] text-muted-foreground',
                  children: node.naming.provider_name
                    ? `Provider name: ${node.naming.provider_name}`
                    : `Provider name unavailable; reset uses stable device ID ${providerFallback}.`
                }),
                jsxs('div', {
                  className: 'flex flex-wrap gap-2',
                  children: [
                    jsx(Button, {
                      type: 'button',
                      size: 'sm',
                      onClick: saveAlias,
                      disabled: !aliasValid || pending || alias === (node.naming.alias ?? ''),
                      children: 'Save alias'
                    }),
                    jsx(Button, {
                      type: 'button',
                      size: 'sm',
                      variant: 'outline',
                      onClick: resetAlias,
                      disabled: !node.naming.has_alias || pending,
                      children: node.naming.provider_name ? 'Reset to provider name' : 'Clear alias'
                    })
                  ]
                }),
                jsx('p', {
                  className: mutation.state === 'error'
                    ? 'text-xs text-destructive'
                    : 'text-xs text-muted-foreground',
                  role: mutation.state === 'error' ? 'alert' : undefined,
                  'aria-live': 'polite',
                  children: mutation.message
                })
              ]
            })
          }),
          jsx(InspectorSection, {
            title: 'Readiness ladder',
            children: jsx(ReadinessLadder, { node })
          }),
          jsx(InspectorSection, {
            title: 'Why not ready',
            children: reasons.length
              ? jsx('ul', {
                  className: 'grid gap-1 text-xs text-foreground',
                  children: reasons.map(reason =>
                    jsx('li', { children: describeReadinessReason(reason) }, reason)
                  )
                })
              : jsx('p', { className: 'text-xs text-muted-foreground', children: 'No readiness blockers.' })
          }),
          jsx(InspectorSection, {
            title: 'Capacity and resources',
            children: jsx('dl', {
              className: 'grid gap-2',
              children: resources.map(row =>
                jsx(InspectorRow, { label: row.label, value: row.value }, row.key)
              )
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
              : jsx('p', { className: 'text-xs text-muted-foreground', children: 'No operations advertised.' })
          }),
          jsx('details', {
            className: 'border-t border-border pt-3',
            children: [
              jsx('summary', {
                className: 'cursor-pointer text-xs font-semibold text-foreground',
                children: 'Technical details'
              }),
              jsx('pre', {
                className: 'mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border p-2 text-[0.6875rem] text-muted-foreground',
                children: JSON.stringify(node, null, 2)
              })
            ]
          })
        ]
      })
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

function FleetCanvasWorkspace({ overview, ctx, refresh, activity }) {
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
  const selectedNode =
    overview.nodes.find(node => node.stable_id === selectedId) ?? overview.nodes[0]
  const animatedIds = useMemo(
    () => new Set(activity.slice(0, 8).map(entry => entry.node_id)),
    [activity]
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
      jsxs('div', {
        className: 'flex min-h-0 flex-1 flex-col gap-3 overflow-auto lg:flex-row lg:overflow-hidden',
        children: [
          jsx(FleetCanvas, {
            graph: visibleGraph,
            positions,
            setPositions,
            commitPositions,
            selectedId,
            setSelectedId,
            animatedIds
          }),
          jsx(NodeInspector, { node: selectedNode, ctx, refresh })
        ]
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

function ConnectionChip({ state }) {
  const tone = state === 'live' ? 'good' : state === 'reconnecting' ? 'warn' : 'muted'
  const label = state === 'live' ? 'Live' : state === 'reconnecting' ? 'Reconnecting' : 'Polling'
  return jsxs('span', {
    className: 'inline-flex items-center gap-1.5 text-xs text-muted-foreground',
    role: 'status',
    'aria-live': 'polite',
    children: [jsx(StatusDot, { tone }), label]
  })
}

function ActivityDrawer({ activity, onClear }) {
  return jsxs('section', {
    className: 'max-h-48 overflow-auto border-b border-border bg-muted/20 px-5 py-3',
    'aria-label': 'Fleet activity',
    children: [
      jsxs('div', {
        className: 'mb-2 flex items-center justify-between',
        children: [
          jsx('h2', { className: 'text-sm font-semibold text-foreground', children: 'Activity' }),
          jsx(Button, { type: 'button', variant: 'ghost', size: 'sm', onClick: onClear, disabled: !activity.length, children: 'Clear' })
        ]
      }),
      activity.length
        ? jsx('ol', {
            className: 'grid gap-1 text-xs text-muted-foreground',
            children: activity.map(entry => jsx('li', { children: entry.message }, entry.id))
          })
        : jsx('p', { className: 'text-xs text-muted-foreground', children: 'No state transitions observed in this session.' })
    ]
  })
}

function FleetStatusChip({ ctx }) {
  const query = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => ctx.rest('/overview'),
    refetchInterval: 15_000,
    retry: 1
  })
  if (!query.data) return jsxs('span', { className: 'inline-flex items-center gap-1.5 text-xs', children: [jsx(StatusDot, { tone: 'muted' }), 'Fleet unavailable'] })
  return jsxs('span', {
    className: 'inline-flex items-center gap-1.5 text-xs',
    children: [jsx(StatusDot, { tone: query.data.summary.not_ready ? 'warn' : 'good' }), `Fleet ${query.data.summary.ready}/${query.data.summary.managed} ready`]
  })
}

function FleetPage({ ctx }) {
  const events = useFleetEvents(ctx)
  const [activityOpen, setActivityOpen] = useState(false)
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
    return jsxs('main', {
      className: 'flex h-full min-h-64 flex-col',
      children: [
        jsx('header', {
          className: 'flex justify-end border-b border-border px-5 py-3',
          children: jsx(ConnectionChip, { state: events.connection })
        }),
        jsx('div', {
          className: 'grid flex-1 place-items-center p-6',
          children: jsx(EmptyState, {
            title: 'Your Fleet is empty',
            description: 'Managed nodes will appear here as they join Fleet.'
          })
        })
      ]
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
              jsx(SummaryItem, { label: 'Needs attention', value: overview.summary.not_ready }),
              jsx(ConnectionChip, { state: events.connection }),
              jsx(Button, {
                type: 'button',
                size: 'sm',
                variant: activityOpen ? 'secondary' : 'outline',
                'aria-expanded': activityOpen,
                onClick: () => setActivityOpen(value => !value),
                children: `Activity (${events.activity.length})`
              })
            ]
          })
        ]
      }),
      activityOpen ? jsx(ActivityDrawer, { activity: events.activity, onClear: events.clearActivity }) : null,
      jsx(FleetCanvasWorkspace, { overview, ctx, refresh: query.refetch, activity: events.activity })
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
    ctx.register({
      id: 'status',
      area: STATUSBAR_AREAS.right,
      order: 55,
      render: () => jsx(FleetStatusChip, { ctx })
    })
    ctx.register({
      id: 'open-command',
      area: PALETTE_AREA,
      data: {
        id: 'fleet.open',
        label: 'Fleet: Open Canvas',
        keywords: ['fleet', 'nodes', 'readiness', 'canvas'],
        run: () => host.navigate('/fleet')
      }
    })
    ctx.register({
      id: 'refresh-command',
      area: PALETTE_AREA,
      data: {
        id: 'fleet.refresh',
        label: 'Fleet: Refresh Overview',
        keywords: ['fleet', 'refresh', 'reconnect'],
        run: async () => {
          await queryClient.invalidateQueries({ queryKey: QUERY_KEY })
          host.notify({ kind: 'info', message: 'Fleet overview refreshed.' })
        }
      }
    })
  }
}
