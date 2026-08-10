import {
  Button,
  Codicon,
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
  ErrorState,
  Loader,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  ScrollArea,
  SearchField,
  SegmentedControl,
  StatusDot,
  host,
  queryClient,
  useQuery
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const QUERY_KEY = ['hermes-fleet', 'desktop', 'overview']
const LAYOUT_STORAGE_KEY = 'topology-layout.v1'
const NODE_WIDTH = 220
const NODE_HEIGHT = 116
const NODE_STEP_X = 284
const NODE_STEP_Y = 176
const MIN_SCALE = 0.5
const MAX_SCALE = 2.5
const POSITION_LIMIT = 100_000
const POSITION_LIMIT_COUNT = 512
const WORKFLOW_LIMIT_COUNT = 256
const EVENT_SCHEMA = 'fleet.desktop-events.v1'

export const FLEET_NODE_TYPE_CATEGORIES = Object.freeze([
  'machine',
  'trigger',
  'fleet-action',
  'hermes-action',
  'flow-control',
  'condition',
  'data',
  'integration',
  'human-approval'
])

const FLEET_PORT_KINDS = Object.freeze([
  'control',
  'data',
  'machine-target',
  'success',
  'error',
  'event',
  'result'
])

function isPlainRecord(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const prototype = Object.getPrototypeOf(value)
  return prototype === Object.prototype || prototype === null
}

const CONFIGURATION_PROPERTY_LIMIT = 32
const CONFIGURATION_ENUM_LIMIT = 32
const CONFIGURATION_STRING_LIMIT = 4096

function configurationSchema(properties = {}) {
  return Object.freeze({
    type: 'object',
    properties: Object.freeze(Object.fromEntries(
      Object.entries(properties)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, schema]) => [key, Object.freeze({
          ...schema,
          ...(schema.enum ? { enum: Object.freeze([...schema.enum]) } : {})
        })])
    )),
    additionalProperties: false
  })
}

const EMPTY_CONFIGURATION_SCHEMA = configurationSchema()

function validConfigurationSchema(schema) {
  if (
    !isPlainRecord(schema) ||
    schema.type !== 'object' ||
    schema.additionalProperties !== false ||
    !isPlainRecord(schema.properties) ||
    Object.keys(schema).some(key => !['type', 'properties', 'additionalProperties'].includes(key)) ||
    Object.keys(schema.properties).length > CONFIGURATION_PROPERTY_LIMIT
  ) return false
  return Object.entries(schema.properties).every(([key, property]) =>
    /^[a-z][a-z0-9_]{0,63}$/.test(key) &&
    isPlainRecord(property) &&
    ['string', 'number', 'boolean'].includes(property.type) &&
    Object.keys(property).every(name => ['type', 'enum', 'minimum', 'maximum'].includes(name)) &&
    (property.enum === undefined || (
      Array.isArray(property.enum) &&
      property.enum.length > 0 && property.enum.length <= CONFIGURATION_ENUM_LIMIT &&
      property.enum.every(value =>
        typeof value === property.type &&
        (typeof value !== 'string' || value.length <= CONFIGURATION_STRING_LIMIT)
      )
    )) &&
    (property.minimum === undefined || (property.type === 'number' && Number.isFinite(property.minimum))) &&
    (property.maximum === undefined || (property.type === 'number' && Number.isFinite(property.maximum))) &&
    (
      property.minimum === undefined || property.maximum === undefined ||
      property.minimum <= property.maximum
    )
  )
}

function canonicalConfigurationSchema(schema) {
  if (!validConfigurationSchema(schema)) return null
  return configurationSchema(schema.properties)
}

function normalizeWorkflowConfiguration(descriptor, value) {
  const configuration = value ?? {}
  if (!isPlainRecord(configuration)) throw new Error('invalid workflow configuration')
  const properties = descriptor.configurationSchema.properties
  const keys = Object.keys(configuration).sort()
  if (keys.some(key => !(key in properties))) throw new Error('invalid workflow configuration')
  const normalized = {}
  for (const key of keys) {
    const field = properties[key]
    const candidate = configuration[key]
    if (
      typeof candidate !== field.type ||
      (field.type === 'number' && !Number.isFinite(candidate)) ||
      (field.type === 'string' && candidate.length > CONFIGURATION_STRING_LIMIT) ||
      (field.enum && !field.enum.includes(candidate)) ||
      (field.minimum !== undefined && candidate < field.minimum) ||
      (field.maximum !== undefined && candidate > field.maximum)
    ) throw new Error('invalid workflow configuration')
    normalized[key] = candidate
  }
  return normalized
}

function fleetPort(id, direction, kind, label) {
  return Object.freeze({ id, direction, kind, label })
}

const PORTS = Object.freeze({
  controlIn: fleetPort('control', 'input', 'control', 'Control'),
  controlOut: fleetPort('control', 'output', 'control', 'Control'),
  dataIn: fleetPort('data', 'input', 'data', 'Data'),
  dataOut: fleetPort('data', 'output', 'data', 'Data'),
  machineIn: fleetPort('machine', 'input', 'machine-target', 'Machine'),
  machineOut: fleetPort('machine', 'output', 'machine-target', 'Machine'),
  success: fleetPort('success', 'output', 'success', 'Success'),
  error: fleetPort('error', 'output', 'error', 'Error'),
  event: fleetPort('event', 'output', 'event', 'Event'),
  result: fleetPort('result', 'output', 'result', 'Result')
})

function defineFleetNodeType(id, label, category, icon, options = {}) {
  const inputs = Object.freeze([...(options.inputs ?? [])])
  const outputs = Object.freeze([...(options.outputs ?? [])])
  return Object.freeze({
    id,
    label,
    category,
    icon,
    accent: options.accent ?? category,
    availability: options.availability ?? 'editor-only',
    runtime: options.runtime ?? 'unavailable',
    inputs,
    outputs,
    defaultPorts: Object.freeze([...inputs, ...outputs]),
    configurationSchema: options.configurationSchema ?? EMPTY_CONFIGURATION_SCHEMA
  })
}

const EDITOR_NODE_TYPE_DEFINITIONS = [
  defineFleetNodeType('machine', 'Machine', 'machine', 'server-process', {
    availability: 'production', runtime: 'evidence-only'
  }),
  defineFleetNodeType('exact-machine', 'Exact Machine', 'machine', 'target', {
    outputs: [PORTS.machineOut]
  }),
  defineFleetNodeType('machine-group', 'Machine Group', 'machine', 'organization', {
    outputs: [PORTS.machineOut]
  }),
  defineFleetNodeType('manual-trigger', 'Manual Trigger', 'trigger', 'play', {
    outputs: [PORTS.controlOut]
  }),
  defineFleetNodeType('schedule', 'Schedule', 'trigger', 'calendar', {
    outputs: [PORTS.event],
    configurationSchema: configurationSchema({ schedule: { type: 'string' } })
  }),
  defineFleetNodeType('fleet-event', 'Fleet Event', 'trigger', 'pulse', {
    outputs: [PORTS.event]
  }),
  defineFleetNodeType('node-online', 'Node Online', 'trigger', 'circle-filled', {
    outputs: [PORTS.event]
  }),
  defineFleetNodeType('node-offline', 'Node Offline', 'trigger', 'circle-slash', {
    outputs: [PORTS.event]
  }),
  defineFleetNodeType('node-ready', 'Node Ready', 'trigger', 'pass-filled', {
    outputs: [PORTS.event]
  }),
  defineFleetNodeType('run-completed', 'Run Completed', 'trigger', 'check-all', {
    outputs: [PORTS.event, PORTS.result]
  }),
  defineFleetNodeType('webhook', 'Webhook', 'trigger', 'radio-tower', {
    outputs: [PORTS.event, PORTS.dataOut]
  }),
  defineFleetNodeType('file-event', 'File Event', 'trigger', 'file', {
    outputs: [PORTS.event, PORTS.dataOut]
  }),
  defineFleetNodeType('find-ready-machine', 'Find Ready Machine', 'fleet-action', 'search', {
    inputs: [PORTS.controlIn], outputs: [PORTS.machineOut, PORTS.error]
  }),
  defineFleetNodeType('find-ready-gpu-machine', 'Find Ready GPU Machine', 'fleet-action', 'search', {
    inputs: [PORTS.controlIn], outputs: [PORTS.machineOut, PORTS.error]
  }),
  defineFleetNodeType('send-message', 'Send Message', 'fleet-action', 'comment', {
    inputs: [PORTS.controlIn, PORTS.machineIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('broadcast', 'Broadcast', 'fleet-action', 'megaphone', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('reserve-capacity', 'Reserve Capacity', 'fleet-action', 'lock', {
    inputs: [PORTS.controlIn, PORTS.machineIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('release-capacity', 'Release Capacity', 'fleet-action', 'unlock', {
    inputs: [PORTS.controlIn, PORTS.machineIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('wait-for-machine', 'Wait for Machine', 'fleet-action', 'watch', {
    inputs: [PORTS.controlIn, PORTS.machineIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('get-machine-status', 'Get Machine Status', 'fleet-action', 'info', {
    inputs: [PORTS.controlIn, PORTS.machineIn], outputs: [PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('start-agent', 'Start Agent', 'hermes-action', 'hubot', {
    inputs: [PORTS.controlIn, PORTS.machineIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('continue-run', 'Continue Run', 'hermes-action', 'debug-continue', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('run-profile', 'Run Profile', 'hermes-action', 'account', {
    inputs: [PORTS.controlIn, PORTS.machineIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('delegate', 'Delegate', 'hermes-action', 'organization', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('tool-call', 'Tool Call', 'hermes-action', 'tools', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('extract-result', 'Extract Result', 'hermes-action', 'symbol-field', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('stop-run', 'Stop Run', 'hermes-action', 'debug-stop', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('wait-for-agent', 'Wait for Agent', 'hermes-action', 'clock', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('if', 'If', 'condition', 'symbol-boolean', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('switch', 'Switch', 'condition', 'list-tree', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('fan-out', 'Fan Out', 'flow-control', 'split-horizontal', {
    inputs: [PORTS.controlIn], outputs: [PORTS.controlOut]
  }),
  defineFleetNodeType('join', 'Join', 'flow-control', 'combine', {
    inputs: [PORTS.controlIn], outputs: [PORTS.controlOut]
  }),
  defineFleetNodeType('loop', 'Loop', 'flow-control', 'sync', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.controlOut, PORTS.dataOut]
  }),
  defineFleetNodeType('retry', 'Retry', 'flow-control', 'refresh', {
    inputs: [PORTS.controlIn], outputs: [PORTS.controlOut, PORTS.error]
  }),
  defineFleetNodeType('delay', 'Delay', 'flow-control', 'watch', {
    inputs: [PORTS.controlIn], outputs: [PORTS.controlOut],
    configurationSchema: configurationSchema({ seconds: { type: 'number', minimum: 0 } })
  }),
  defineFleetNodeType('timeout', 'Timeout', 'flow-control', 'history', {
    inputs: [PORTS.controlIn], outputs: [PORTS.success, PORTS.error],
    configurationSchema: configurationSchema({ seconds: { type: 'number', minimum: 0 } })
  }),
  defineFleetNodeType('error-handler', 'Error Handler', 'flow-control', 'warning', {
    inputs: [fleetPort('error', 'input', 'error', 'Error')], outputs: [PORTS.controlOut]
  }),
  defineFleetNodeType('json', 'JSON', 'data', 'json', {
    outputs: [PORTS.dataOut]
  }),
  defineFleetNodeType('transform', 'Transform', 'data', 'symbol-method', {
    inputs: [PORTS.dataIn], outputs: [PORTS.dataOut, PORTS.error]
  }),
  defineFleetNodeType('filter', 'Filter', 'data', 'filter', {
    inputs: [PORTS.dataIn], outputs: [PORTS.dataOut]
  }),
  defineFleetNodeType('merge', 'Merge', 'data', 'combine', {
    inputs: [PORTS.dataIn], outputs: [PORTS.dataOut]
  }),
  defineFleetNodeType('extract-field', 'Extract Field', 'data', 'symbol-field', {
    inputs: [PORTS.dataIn], outputs: [PORTS.dataOut, PORTS.error]
  }),
  defineFleetNodeType('template', 'Template', 'data', 'symbol-string', {
    inputs: [PORTS.dataIn], outputs: [PORTS.dataOut],
    configurationSchema: configurationSchema({ template: { type: 'string' } })
  }),
  defineFleetNodeType('approval', 'Approval', 'human-approval', 'verified', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.error]
  }),
  defineFleetNodeType('prompt-operator', 'Prompt Operator', 'human-approval', 'comment-discussion', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('wait-for-input', 'Wait for Input', 'human-approval', 'question', {
    inputs: [PORTS.controlIn], outputs: [PORTS.result, PORTS.error]
  }),
  defineFleetNodeType('http', 'HTTP', 'integration', 'globe', {
    inputs: [PORTS.controlIn, PORTS.dataIn], outputs: [PORTS.success, PORTS.result, PORTS.error],
    configurationSchema: configurationSchema({
      method: { type: 'string', enum: ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] },
      url: { type: 'string' }
    })
  })
]

export const FLEET_NODE_TYPES = Object.freeze(
  Object.fromEntries(EDITOR_NODE_TYPE_DEFINITIONS.map(descriptor => [descriptor.id, descriptor]))
)

export function getFleetNodeType(value) {
  return FLEET_NODE_TYPES[value] ?? null
}

function validContributionPorts(inputs, outputs) {
  if (!Array.isArray(inputs) || !Array.isArray(outputs)) return false
  const used = new Set()
  return [
    ...inputs.map(port => [port, 'input']),
    ...outputs.map(port => [port, 'output'])
  ].every(([port, direction]) => {
    if (
      !isPlainRecord(port) ||
      typeof port.id !== 'string' ||
      !/^[a-z][a-z0-9-]{0,63}$/.test(port.id) ||
      used.has(port.id) ||
      port.direction !== direction ||
      !FLEET_PORT_KINDS.includes(port.kind) ||
      typeof port.label !== 'string' ||
      !port.label.trim() ||
      port.label.length > 64 ||
      Object.keys(port).some(key => !['id', 'direction', 'kind', 'label'].includes(key))
    ) return false
    used.add(port.id)
    return true
  })
}

export function createFleetNodeRegistry(contributions = [], options = {}) {
  if (!Array.isArray(contributions) || contributions.length > 64) {
    throw new Error('invalid node contributions')
  }
  const registry = new Map(Object.entries(FLEET_NODE_TYPES))
  const contributionKeys = new Set([
    'id', 'label', 'category', 'icon', 'inputs', 'outputs', 'configurationSchema'
  ])
  const iconValidator = typeof options.iconValidator === 'function'
    ? options.iconValidator
    : value => /^[a-z][a-z0-9-]{0,63}$/.test(value)
  for (const contribution of contributions) {
    const inputs = contribution?.inputs ?? []
    const outputs = contribution?.outputs ?? []
    const schema = canonicalConfigurationSchema(
      contribution?.configurationSchema ?? EMPTY_CONFIGURATION_SCHEMA
    )
    if (
      !isPlainRecord(contribution) ||
      Object.keys(contribution).some(key => !contributionKeys.has(key)) ||
      typeof contribution.id !== 'string' ||
      !/^[a-z][a-z0-9-]{1,63}$/.test(contribution.id) ||
      registry.has(contribution.id) ||
      !FLEET_NODE_TYPE_CATEGORIES.includes(contribution.category) ||
      typeof contribution.label !== 'string' ||
      !contribution.label.trim() ||
      contribution.label.length > 64 ||
      typeof contribution.icon !== 'string' ||
      !iconValidator(contribution.icon) ||
      !validContributionPorts(inputs, outputs) ||
      !schema
    ) continue
    const descriptor = defineFleetNodeType(
      contribution.id,
      contribution.label.trim(),
      contribution.category,
      contribution.icon,
      {
        inputs,
        outputs,
        configurationSchema: schema,
        availability: 'editor-only',
        runtime: 'unavailable'
      }
    )
    registry.set(descriptor.id, descriptor)
  }
  return registry
}

const WORKFLOW_SCHEMA = 'fleet.workflow-editor.v1'
const WORKFLOW_CLIPBOARD_SCHEMA = 'fleet.workflow-clipboard.v1'

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value))
}

function validWorkflowId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)
}

function validWorkflowPosition(value) {
  return Boolean(
    isPlainRecord(value) &&
    Object.keys(value).length === 2 &&
    'x' in value &&
    'y' in value &&
    finitePosition(value.x) &&
    finitePosition(value.y)
  )
}

function hasExactKeys(value, keys) {
  return isPlainRecord(value) &&
    Object.keys(value).length === keys.length &&
    keys.every(key => key in value)
}

function validTargetText(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= 256
}

function normalizeWorkflowTarget(value) {
  if (value == null) return null
  if (value.authority === 'observed') {
    const keys = [
      'stable_id', 'authority', 'provider', 'provider_instance_id',
      'provider_node_id', 'network_id', 'observed_id'
    ]
    if (
      !hasExactKeys(value, keys) ||
      !validWorkflowId(value.stable_id) ||
      !validTargetText(value.provider) ||
      !validTargetText(value.provider_instance_id) ||
      !validTargetText(value.provider_node_id) ||
      !validTargetText(value.network_id) ||
      !/^sha256:[0-9a-f]{64}$/.test(value.observed_id) ||
      value.stable_id !== `observed-node-${value.observed_id.slice(7)}`
    ) throw new Error('invalid workflow target')
    return Object.fromEntries(keys.map(key => [key, value[key]]))
  }
  if (value.authority === 'managed') {
    const keys = ['stable_id', 'authority', 'source', 'network_id', 'device_id']
    if (
      !hasExactKeys(value, keys) ||
      !validWorkflowId(value.stable_id) ||
      !validTargetText(value.source) ||
      !validTargetText(value.network_id) ||
      !validTargetText(value.device_id)
    ) throw new Error('invalid workflow target')
    return Object.fromEntries(keys.map(key => [key, value[key]]))
  }
  throw new Error('invalid workflow target')
}

function workflowNodeFromInput(input) {
  const descriptor = getFleetNodeType(input?.type)
  const allowedKeys = ['id', 'type', 'title', 'position', 'configuration', 'target', 'runtime']
  if (
    !isPlainRecord(input) ||
    !descriptor ||
    descriptor.runtime !== 'unavailable' ||
    !validWorkflowId(input.id) ||
    !validWorkflowPosition(input.position) ||
    Object.keys(input).some(key => !allowedKeys.includes(key)) ||
    (input.title !== undefined && typeof input.title !== 'string') ||
    (input.runtime !== undefined && input.runtime !== descriptor.runtime) ||
    (input.target != null && descriptor.id !== 'exact-machine')
  ) {
    throw new Error('invalid workflow node')
  }
  const target = normalizeWorkflowTarget(input.target)
  return {
    id: input.id,
    type: descriptor.id,
    title: typeof input.title === 'string' && input.title.trim()
      ? input.title.trim().slice(0, 128)
      : descriptor.label,
    position: { x: input.position.x, y: input.position.y },
    configuration: normalizeWorkflowConfiguration(descriptor, input.configuration),
    target,
    runtime: descriptor.runtime
  }
}

export function createEmptyWorkflow(id = 'untitled-workflow') {
  if (!validWorkflowId(id)) throw new Error('invalid workflow id')
  return {
    schema: WORKFLOW_SCHEMA,
    id,
    name: 'Untitled workflow',
    nodes: [],
    connections: [],
    metadata: { executionAvailable: false }
  }
}

export function addWorkflowNode(workflow, input) {
  if (workflow.nodes.length >= WORKFLOW_LIMIT_COUNT) throw new Error('workflow node limit reached')
  if (workflow.nodes.some(node => node.id === input.id)) throw new Error('duplicate workflow node')
  return { ...workflow, nodes: [...workflow.nodes, workflowNodeFromInput(input)] }
}

function workflowConnectionKind(sourcePort, targetPort) {
  if (sourcePort.kind === targetPort.kind) return sourcePort.kind
  if (['event', 'success', 'error'].includes(sourcePort.kind) && targetPort.kind === 'control') {
    return sourcePort.kind
  }
  if (sourcePort.kind === 'result' && targetPort.kind === 'data') return 'data'
  return null
}

export function connectWorkflowNodes(workflow, input) {
  if (workflow.connections.length >= WORKFLOW_LIMIT_COUNT) {
    throw new Error('workflow connection limit reached')
  }
  const allowedKeys = ['id', 'source', 'sourcePort', 'target', 'targetPort', 'kind']
  if (
    !isPlainRecord(input) ||
    Object.keys(input).some(key => !allowedKeys.includes(key)) ||
    !validWorkflowId(input.id) ||
    workflow.connections.some(edge => edge.id === input.id)
  ) {
    throw new Error('invalid workflow connection')
  }
  const sourceNode = workflow.nodes.find(node => node.id === input.source)
  const targetNode = workflow.nodes.find(node => node.id === input.target)
  if (!sourceNode || !targetNode || sourceNode.id === targetNode.id) {
    throw new Error('invalid workflow connection endpoints')
  }
  const sourceDescriptor = getFleetNodeType(sourceNode.type)
  const targetDescriptor = getFleetNodeType(targetNode.type)
  const sourcePort = sourceDescriptor.outputs.find(port => port.id === input.sourcePort)
  const targetPort = targetDescriptor.inputs.find(port => port.id === input.targetPort)
  const kind = sourcePort && targetPort
    ? workflowConnectionKind(sourcePort, targetPort)
    : null
  if (!kind) throw new Error('incompatible workflow ports')
  if (input.kind !== undefined && input.kind !== kind) {
    throw new Error('invalid workflow connection kind')
  }
  return {
    ...workflow,
    connections: [...workflow.connections, {
      id: input.id,
      source: sourceNode.id,
      sourcePort: sourcePort.id,
      target: targetNode.id,
      targetPort: targetPort.id,
      kind
    }]
  }
}

export function updateFleetSelection(selection, id, options = {}) {
  const current = new Set(Array.isArray(selection) ? selection : [])
  if (options.toggle) {
    if (current.has(id)) current.delete(id)
    else current.add(id)
  } else {
    current.clear()
    if (id) current.add(id)
  }
  return [...current]
}

export function nodesInsideSelection(nodes, bounds) {
  if (!bounds || !Number.isFinite(bounds.x) || !Number.isFinite(bounds.y)) return []
  const right = bounds.x + Math.max(0, bounds.width)
  const bottom = bounds.y + Math.max(0, bounds.height)
  return nodes
    .filter(node =>
      node.x >= bounds.x &&
      node.y >= bounds.y &&
      node.x + node.width <= right &&
      node.y + node.height <= bottom
    )
    .map(node => node.id)
}

export function deleteWorkflowSelection(workflow, selection) {
  const selected = new Set(selection)
  return {
    ...workflow,
    nodes: workflow.nodes.filter(node => !selected.has(node.id)),
    connections: workflow.connections.filter(edge =>
      !selected.has(edge.source) && !selected.has(edge.target)
    )
  }
}

export function copyWorkflowSelection(workflow, selection) {
  const selected = new Set(selection)
  return {
    schema: WORKFLOW_CLIPBOARD_SCHEMA,
    nodes: cloneJson(workflow.nodes.filter(node => selected.has(node.id))),
    connections: cloneJson(workflow.connections.filter(edge =>
      selected.has(edge.source) && selected.has(edge.target)
    ))
  }
}

function normalizeWorkflowClipboard(clipboard) {
  if (
    !hasExactKeys(clipboard, ['schema', 'nodes', 'connections']) ||
    clipboard.schema !== WORKFLOW_CLIPBOARD_SCHEMA ||
    !Array.isArray(clipboard.nodes) ||
    !Array.isArray(clipboard.connections) ||
    clipboard.nodes.length > WORKFLOW_LIMIT_COUNT ||
    clipboard.connections.length > WORKFLOW_LIMIT_COUNT
  ) throw new Error('invalid workflow clipboard')
  let normalized = createEmptyWorkflow('clipboard-validation')
  try {
    for (const node of clipboard.nodes) normalized = addWorkflowNode(normalized, node)
    for (const edge of clipboard.connections) normalized = connectWorkflowNodes(normalized, edge)
  } catch {
    throw new Error('invalid workflow clipboard')
  }
  return normalized
}

function allocateWorkflowId(base, used) {
  for (let attempt = 0; attempt <= WORKFLOW_LIMIT_COUNT; attempt += 1) {
    const suffix = attempt ? `-${attempt + 1}` : ''
    const candidate = `${base.slice(0, 128 - suffix.length)}${suffix}`
    if (validWorkflowId(candidate) && !used.has(candidate)) {
      used.add(candidate)
      return candidate
    }
  }
  throw new Error('workflow id allocation failed')
}

export function pasteWorkflowClipboard(workflow, clipboard, options = {}) {
  const source = normalizeWorkflowClipboard(clipboard)
  const offset = options.offset ?? { x: 32, y: 32 }
  if (!validWorkflowPosition(offset)) throw new Error('invalid workflow clipboard offset')
  if (
    workflow.nodes.length + source.nodes.length > WORKFLOW_LIMIT_COUNT ||
    workflow.connections.length + source.connections.length > WORKFLOW_LIMIT_COUNT
  ) throw new Error('workflow clipboard limit reached')
  const prefix = validWorkflowId(options.idPrefix) ? options.idPrefix : 'copy'
  const nodeIds = new Set(workflow.nodes.map(node => node.id))
  const edgeIds = new Set(workflow.connections.map(edge => edge.id))
  const idMap = new Map()
  let next = workflow
  source.nodes.forEach(node => {
    const id = allocateWorkflowId(`${prefix}-${node.id}`, nodeIds)
    idMap.set(node.id, id)
    next = addWorkflowNode(next, {
      ...node,
      id,
      position: {
        x: node.position.x + offset.x,
        y: node.position.y + offset.y
      }
    })
  })
  for (const edge of source.connections) {
    next = connectWorkflowNodes(next, {
      ...edge,
      id: allocateWorkflowId(`${prefix}-${edge.id}`, edgeIds),
      source: idMap.get(edge.source),
      target: idMap.get(edge.target)
    })
  }
  return next
}

export function duplicateWorkflowSelection(workflow, selection, options = {}) {
  return pasteWorkflowClipboard(
    workflow,
    copyWorkflowSelection(workflow, selection),
    options
  )
}

export function serializeWorkflow(workflow) {
  return JSON.stringify(deserializeWorkflow(workflow))
}

function assertUniqueJsonMembers(value) {
  const stack = []
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index]
    if (character === '{') stack.push(new Set())
    else if (character === '[') stack.push(null)
    else if (character === '}' || character === ']') stack.pop()
    else if (character === '"') {
      const start = index
      index += 1
      while (index < value.length) {
        if (value[index] === '\\') index += 2
        else if (value[index] === '"') break
        else index += 1
      }
      let cursor = index + 1
      while (/\s/.test(value[cursor] ?? '')) cursor += 1
      const keys = stack.at(-1)
      if (keys && value[cursor] === ':') {
        const key = JSON.parse(value.slice(start, index + 1))
        if (keys.has(key)) throw new Error('duplicate workflow document member')
        keys.add(key)
      }
    }
  }
}

export function deserializeWorkflow(value) {
  if (typeof value === 'string') assertUniqueJsonMembers(value)
  const parsed = typeof value === 'string' ? JSON.parse(value) : cloneJson(value)
  if (
    !hasExactKeys(parsed, ['schema', 'id', 'name', 'nodes', 'connections', 'metadata']) ||
    parsed.schema !== WORKFLOW_SCHEMA ||
    !validWorkflowId(parsed.id) ||
    typeof parsed.name !== 'string' ||
    !hasExactKeys(parsed.metadata, ['executionAvailable']) ||
    parsed.metadata.executionAvailable !== false ||
    !Array.isArray(parsed.nodes) ||
    !Array.isArray(parsed.connections) ||
    parsed.nodes.length > WORKFLOW_LIMIT_COUNT ||
    parsed.connections.length > WORKFLOW_LIMIT_COUNT
  ) throw new Error('invalid workflow document')
  let workflow = createEmptyWorkflow(parsed.id)
  workflow = { ...workflow, name: parsed.name.trim().slice(0, 128) || 'Untitled workflow' }
  for (const node of parsed.nodes) workflow = addWorkflowNode(workflow, node)
  for (const edge of parsed.connections) workflow = connectWorkflowNodes(workflow, edge)
  return workflow
}

function normalizeWorkflowHistory(history) {
  if (
    !hasExactKeys(history, ['past', 'present', 'future']) ||
    !Array.isArray(history.past) ||
    !Array.isArray(history.future) ||
    history.past.length > 64 ||
    history.future.length > 64
  ) throw new Error('invalid workflow history')
  return {
    past: history.past.map(deserializeWorkflow),
    present: deserializeWorkflow(history.present),
    future: history.future.map(deserializeWorkflow)
  }
}

export function createWorkflowHistory(workflow) {
  return { past: [], present: deserializeWorkflow(workflow), future: [] }
}

export function applyWorkflowEdit(history, workflow) {
  const current = normalizeWorkflowHistory(history)
  const present = deserializeWorkflow(workflow)
  return {
    past: [...current.past, current.present].slice(-64),
    present,
    future: []
  }
}

export function undoWorkflow(history) {
  const current = normalizeWorkflowHistory(history)
  if (!current.past.length) return current
  const present = current.past.at(-1)
  return {
    past: current.past.slice(0, -1),
    present,
    future: [current.present, ...current.future].slice(0, 64)
  }
}

export function redoWorkflow(history) {
  const current = normalizeWorkflowHistory(history)
  if (!current.future.length) return current
  return {
    past: [...current.past, current.present].slice(-64),
    present: current.future[0],
    future: current.future.slice(1)
  }
}

function workflowTargetFromTopology(node) {
  if (!isPlainRecord(node)) throw new Error('invalid topology target')
  return node.kind === 'observed'
    ? {
        stable_id: node.stable_id,
        authority: 'observed',
        provider: node.provider.kind,
        provider_instance_id: node.provider.instance_id,
        provider_node_id: node.provider.node_id,
        network_id: node.provider.network_id,
        observed_id: node.observation.observed_id
      }
    : {
        stable_id: node.stable_id,
        authority: 'managed',
        source: node.identity.source,
        network_id: node.identity.network_id,
        device_id: node.identity.device_id
      }
}

export function appendTopologyTargetsToWorkflow(workflow, selectedNodes) {
  if (!Array.isArray(selectedNodes) || selectedNodes.length > WORKFLOW_LIMIT_COUNT) {
    throw new Error('invalid topology selection')
  }
  let next = deserializeWorkflow(workflow)
  const existingTargets = new Set(
    next.nodes.map(node => node.target?.stable_id).filter(Boolean)
  )
  const additions = selectedNodes.filter(node => !existingTargets.has(node.stable_id))
  if (!additions.length) return workflow
  if (next.nodes.length + additions.length > WORKFLOW_LIMIT_COUNT) {
    throw new Error('workflow node limit reached')
  }
  const usedIds = new Set(next.nodes.map(node => node.id))
  for (const node of additions) {
    const target = workflowTargetFromTopology(node)
    const index = next.nodes.length
    next = addWorkflowNode(next, {
      id: allocateWorkflowId(`target-${index + 1}`, usedIds),
      type: 'exact-machine',
      title: node.naming.display_name,
      position: {
        x: (index % 3) * NODE_STEP_X,
        y: Math.floor(index / 3) * NODE_STEP_Y
      },
      target
    })
    existingTargets.add(target.stable_id)
  }
  return next
}

export function createWorkflowFromTopology(id, selectedNodes) {
  const workflow = {
    ...createEmptyWorkflow(id),
    name: 'Workflow from Fleet selection'
  }
  return appendTopologyTargetsToWorkflow(workflow, selectedNodes)
}

const FLEET_CANVAS_STYLES = `
.fleet-canvas-root {
  container: fleet-canvas / inline-size;
  --fleet-surface: color-mix(in srgb, var(--ui-bg-editor) 88%, var(--ui-text-primary) 12%);
  --fleet-surface-raised: color-mix(in srgb, var(--ui-bg-editor) 78%, var(--ui-text-primary) 22%);
  --fleet-surface-soft: color-mix(in srgb, var(--ui-bg-sidebar) 78%, var(--ui-bg-editor) 22%);
  --fleet-line: color-mix(in srgb, var(--ui-stroke-secondary) 72%, transparent);
  --fleet-line-strong: color-mix(in srgb, var(--ui-text-secondary) 40%, var(--ui-stroke-secondary));
  --fleet-selected: color-mix(in srgb, var(--ui-accent) 52%, var(--ui-text-primary) 48%);
  --fleet-observed: color-mix(in srgb, var(--ui-accent) 16%, var(--ui-text-secondary) 84%);
  --fleet-managed: color-mix(in srgb, var(--ui-accent) 34%, var(--ui-text-primary) 66%);
  --fleet-inactive: var(--ui-text-quaternary);
  --fleet-execution: color-mix(in srgb, var(--ui-accent) 58%, var(--ui-text-primary) 42%);
  --fleet-success: var(--ui-green);
  --fleet-attention: var(--ui-yellow);
  --fleet-ready: var(--ui-green);
  --fleet-port-control: var(--ui-text-secondary);
  --fleet-port-data: var(--ui-accent);
  --fleet-port-machine: var(--ui-text-primary);
  --fleet-port-event: var(--ui-yellow);
  --fleet-port-result: var(--ui-accent);
  --fleet-error: var(--ui-red);
}
.fleet-canvas-surface {
  background: var(--ui-bg-editor);
  border: 1px solid var(--fleet-line);
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--ui-text-primary) 4%, transparent);
}
.fleet-canvas-grid {
  background-image: radial-gradient(circle at center, var(--ui-stroke-secondary, var(--border)) 1px, transparent 1.2px);
  background-size: 24px 24px;
  background-position: center;
}
.fleet-node-shell {
  box-sizing: border-box;
  height: 100%;
  width: 100%;
  overflow: hidden;
  border: 1px solid var(--fleet-line);
  border-radius: 14px;
  background-color: var(--ui-bg-sidebar, var(--ui-bg-editor, var(--background)));
  background-image: linear-gradient(145deg, var(--fleet-surface-raised), var(--fleet-surface));
  color: var(--ui-text-primary);
  box-shadow: 0 12px 28px color-mix(in srgb, var(--ui-bg-editor) 54%, transparent), inset 0 1px 0 color-mix(in srgb, var(--ui-text-primary) 7%, transparent);
  transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease, opacity 160ms ease;
  user-select: none;
}
.fleet-node-shell[data-hovered='true'] {
  transform: translateY(-2px);
  border-color: var(--fleet-line-strong);
  box-shadow: 0 16px 34px color-mix(in srgb, var(--ui-bg-editor) 62%, transparent), inset 0 1px 0 color-mix(in srgb, var(--ui-text-primary) 10%, transparent);
}
.fleet-node-shell[data-selected='true'] {
  transform: translateY(-2px);
  border-color: var(--fleet-selected);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--fleet-selected) 28%, transparent), 0 18px 38px color-mix(in srgb, var(--ui-bg-editor) 64%, transparent);
}
.fleet-node-shell[data-disabled='true'] { opacity: 0.82; }
.fleet-node-enter { animation: fleet-node-enter 220ms ease-out both; }
.fleet-node-icon {
  display: grid;
  height: 34px;
  width: 34px;
  place-items: center;
  border-radius: 10px;
  background-color: var(--ui-bg-tertiary, var(--ui-bg-editor, var(--background)));
  color: var(--ui-text-primary);
}
.fleet-node-shell[data-category='trigger'] .fleet-node-icon { color: var(--ui-yellow); }
.fleet-node-shell[data-category='fleet-action'] .fleet-node-icon { color: var(--fleet-managed); }
.fleet-node-shell[data-category='hermes-action'] .fleet-node-icon { color: var(--fleet-execution); }
.fleet-node-shell[data-category='flow-control'] .fleet-node-icon,
.fleet-node-shell[data-category='condition'] .fleet-node-icon { color: var(--ui-text-secondary); }
.fleet-node-shell[data-category='data'] .fleet-node-icon { color: var(--ui-green); }
.fleet-node-shell[data-category='human-approval'] .fleet-node-icon { color: var(--ui-yellow); }
.fleet-node-shell[data-category='integration'] .fleet-node-icon { color: var(--ui-accent); }
.fleet-node-badge {
  display: inline-flex;
  align-items: center;
  min-height: 18px;
  border-radius: 999px;
  padding: 0 7px;
  background-color: var(--ui-bg-tertiary, var(--ui-bg-editor, var(--background)));
  color: var(--ui-text-secondary);
  font-size: 9px;
  font-weight: 650;
  letter-spacing: 0.04em;
}
.fleet-node-badge[data-tone='info'] {
  background: color-mix(in srgb, var(--fleet-observed) 14%, transparent);
  color: color-mix(in srgb, var(--fleet-observed) 72%, var(--ui-text-primary));
}
.fleet-node-badge[data-tone='attention'] {
  background: color-mix(in srgb, var(--fleet-attention) 12%, transparent);
  color: color-mix(in srgb, var(--fleet-attention) 70%, var(--ui-text-primary));
}
.fleet-node-port {
  position: absolute;
  top: 50%;
  height: 10px;
  width: 10px;
  border: 2px solid var(--fleet-surface);
  border-radius: 999px;
  background: var(--fleet-line-strong);
  opacity: 0;
  transform: translateY(-50%);
}
.fleet-node-port[data-direction='input'] { left: -5px; }
.fleet-node-port[data-direction='output'] { right: -5px; }
.fleet-node-port[data-port-kind='control'] { background: var(--fleet-port-control); }
.fleet-node-port[data-port-kind='data'] {
  border-radius: 2px;
  background: var(--fleet-port-data);
}
.fleet-node-port[data-port-kind='machine-target'] {
  border-radius: 2px;
  background: var(--fleet-port-machine);
  transform: translateY(-50%) rotate(45deg);
}
.fleet-node-port[data-port-kind='success'] { background: var(--ui-green); }
.fleet-node-port[data-port-kind='error'] {
  border-radius: 2px;
  background: var(--ui-red);
}
.fleet-node-port[data-port-kind='event'] {
  background: var(--fleet-port-event);
  box-shadow: 0 0 0 1px var(--fleet-surface), 0 0 0 2px var(--fleet-port-event);
}
.fleet-node-port[data-port-kind='result'] {
  border-radius: 3px;
  background: var(--fleet-port-result);
}
.fleet-node-shell[data-show-ports='true'] .fleet-node-port { opacity: 1; }
.fleet-group-region {
  fill: var(--ui-bg-sidebar, var(--ui-bg-editor, var(--background)));
  fill-opacity: 0.72;
  stroke: var(--fleet-line);
  stroke-width: 1;
}
.fleet-group-header {
  color: var(--ui-text-secondary);
  font-size: 11px;
}
.fleet-minimap {
  opacity: 0.72;
  background-color: var(--ui-bg-editor, var(--background));
  transition: opacity 160ms ease, transform 160ms ease;
}
.fleet-minimap[data-inspector-open='true'] {
  right: calc(min(25rem, 56%) + 0.75rem);
  width: 9rem;
}
.fleet-minimap:hover { opacity: 0.9; transform: translateY(-1px); }
.fleet-minimap-viewport {
  fill: var(--fleet-selected);
  fill-opacity: 0.09;
  stroke: var(--fleet-selected);
  stroke-width: 2.5;
  vector-effect: non-scaling-stroke;
}
.fleet-inspector-drawer {
  animation: fleet-drawer-in 180ms ease-out;
  border: 1px solid var(--fleet-line);
  background-color: var(--ui-bg-editor, var(--background));
  box-shadow: -18px 0 44px color-mix(in srgb, var(--ui-bg-editor) 64%, transparent);
  width: min(25rem, 56%);
}
@container fleet-canvas (max-width: 37.5rem) {
  .fleet-inspector-drawer { width: min(20rem, 54%); }
  .fleet-minimap[data-inspector-open='true'] {
    right: calc(min(20rem, 54%) + 1rem);
    width: 7rem;
    height: 7rem;
  }
  .fleet-workflow-palette {
    width: 10rem;
    flex-basis: 10rem;
  }
}
@container fleet-canvas (max-width: 25rem) {
  .fleet-inspector-drawer { width: min(17rem, 60%); }
  .fleet-minimap[data-inspector-open='true'] {
    right: calc(min(17rem, 60%) + 1rem);
    width: 5rem;
    height: 5rem;
  }
  .fleet-workflow-palette {
    width: 8rem;
    flex-basis: 8rem;
  }
}
@keyframes fleet-drawer-in {
  from { opacity: 0; transform: translateX(18px); }
  to { opacity: 1; transform: translateX(0); }
}
@keyframes fleet-node-enter {
  from { opacity: 0; transform: translateY(6px) scale(0.98); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
@media (prefers-reduced-motion: reduce) {
  .fleet-canvas-root *,
  .fleet-canvas-root *::before,
  .fleet-canvas-root *::after {
    animation: none !important;
    scroll-behavior: auto !important;
    transition: none !important;
  }
}
`

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
  useEffect(() =>
    ctx.socket('/events', event => {
      if (!validFleetEvent(event)) return
      if (event.kind === 'unavailable') {
        setConnection('reconnecting')
        void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
        return
      }
      setConnection('live')
      if (event.kind !== 'heartbeat') {
        void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
      }
    }),
  [ctx])
  return { connection }
}

function useFleetActivity(overview) {
  const [activity, setActivity] = useState([])
  const previousRef = useRef(null)
  const sequenceRef = useRef(0)
  useEffect(() => {
    if (
      !overview ||
      !['fleet.desktop.v1', 'fleet.desktop.v2'].includes(overview.schema)
    ) return
    const previous = previousRef.current
    previousRef.current = overview
    if (!previous) return
    sequenceRef.current += 1
    const changes = diffFleetOverview(previous, overview, sequenceRef.current)
    if (changes.length) {
      setActivity(items => [...changes, ...items].slice(0, 64))
    }
  }, [overview])
  return { activity, clearActivity: () => setActivity([]) }
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

function observedState(readiness, hasEvidence, isReady) {
  if (!hasEvidence || !readiness.fresh) return 'unknown'
  return isReady ? 'ready' : 'blocked'
}

function observedDetail(readiness, value) {
  if (value == null) return 'No evidence'
  return readiness.fresh
    ? value
    : `Last observed ${formatFleetAge(readiness.observation_age_ms)}: ${value}`
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
      observedState(readiness, Boolean(observation), observation?.network === 'reachable'),
      observedDetail(readiness, observation?.network)
    ),
    readinessStep(
      'keryx',
      'Keryx',
      observedState(readiness, Boolean(observation), observation?.keryx === 'available'),
      observedDetail(readiness, observation?.keryx)
    ),
    readinessStep(
      'hermes',
      'Hermes',
      observedState(readiness, Boolean(observation), observation?.hermes === 'available'),
      observedDetail(readiness, observation?.hermes)
    ),
    readinessStep(
      'worker',
      'Worker',
      observedState(readiness, Boolean(observation), observation?.worker === 'available'),
      observedDetail(readiness, observation?.worker)
    ),
    readinessStep(
      'capacity',
      'Capacity',
      observedState(readiness, Boolean(capacity), capacity?.available_worker_slots > 0),
      observedDetail(
        readiness,
        capacity ? `${capacity.available_worker_slots} worker slot(s) free` : null
      )
    )
  ]
}

function byteCapacity(value) {
  return value
    ? `${formatFleetBytes(value.available_bytes)} free / ${formatFleetBytes(value.total_bytes)}`
    : 'No evidence'
}

export function formatCanvasCapacity(readiness) {
  const capacity = readiness.capacity
  if (!capacity) return 'No worker capacity'
  const label = `Workers ${capacity.active_workers} / ${capacity.max_workers}`
  return readiness.fresh
    ? label
    : `Last observed ${formatFleetAge(readiness.observation_age_ms)}: ${label}`
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

function providerLabel(value) {
  if (value === 'headscale') return 'Headscale'
  if (value === 'tailscale') return 'Tailscale'
  if (value === 'fake') return 'Fake provider'
  return 'Provider'
}

function machinePresentationName(observation) {
  return observation.hostname || observation.given_name || observation.provider_node_id
}

function projectObservedEvidence(node) {
  return {
    observed_id: node.observed_id,
    network_id: node.network_id,
    provider_kind: node.provider_kind,
    provider_instance_id: node.provider_instance_id,
    provider_node_id: node.provider_node_id,
    hostname: node.hostname,
    given_name: node.given_name,
    addresses: Array.isArray(node.addresses) ? [...node.addresses] : [],
    tags: Array.isArray(node.tags) ? [...node.tags] : [],
    registered_at: node.registered_at,
    last_seen_at: node.last_seen_at,
    expires_at: node.expires_at,
    online: node.online,
    expired: node.expired,
    classification: node.classification,
    first_observed_at: node.first_observed_at,
    last_observed_at: node.last_observed_at,
    snapshot_at: node.snapshot_at
  }
}

export function buildFleetCanvasNodes(overview) {
  const managedRows = Array.isArray(overview?.nodes) ? overview.nodes : []
  const observedRows = Array.isArray(overview?.observed_nodes) ? overview.observed_nodes : []
  if (managedRows.length > 256 || observedRows.length > 256) {
    throw new Error('Fleet overview exceeds bounded node collections')
  }
  const managed = managedRows.map(node => ({ ...node, kind: 'managed', node_type: 'machine' }))
  const observed = observedRows
        .filter(node => /^sha256:[0-9a-f]{64}$/.test(node?.observed_id ?? ''))
        .map(projectObservedEvidence)
        .map(observation => ({
          kind: 'observed',
          node_type: 'machine',
          stable_id: `observed-node-${observation.observed_id.slice(7)}`,
          naming: {
            display_name: machinePresentationName(observation),
            technical_name: observation.given_name || observation.hostname || observation.provider_node_id
          },
          provider: {
            label: providerLabel(observation.provider_kind),
            kind: observation.provider_kind,
            instance_id: observation.provider_instance_id,
            node_id: observation.provider_node_id,
            network_id: observation.network_id
          },
          observation
        }))
  return [...managed, ...observed]
}

function fleetGroupDescriptor(node) {
  if (node.kind === 'observed') {
    return {
      id: `observed:${node.provider.kind}:${node.provider.network_id}:${node.provider.instance_id}`,
      label: `${node.provider.label} network`,
      kind: 'observed'
    }
  }
  return {
    id: `managed:${node.identity.source}:${node.identity.network_id}`,
    label: `Managed · ${node.identity.source} · ${node.identity.network_id}`,
    kind: 'managed'
  }
}

function buildFleetGroups(nodes) {
  const groups = new Map()
  for (const node of nodes) {
    const current = groups.get(node.group.id) ?? { ...node.group, nodes: [] }
    current.nodes.push(node)
    groups.set(node.group.id, current)
  }
  return [...groups.values()]
    .sort((left, right) => compareIds(left.id, right.id))
    .map(group => {
      const left = Math.min(...group.nodes.map(node => node.x)) - 28
      const top = Math.min(...group.nodes.map(node => node.y)) - 42
      const right = Math.max(...group.nodes.map(node => node.x + node.width)) + 28
      const bottom = Math.max(...group.nodes.map(node => node.y + node.height)) + 28
      return {
        id: group.id,
        label: group.label,
        kind: group.kind,
        nodeIds: group.nodes.map(node => node.id),
        x: left,
        y: top,
        width: right - left,
        height: bottom - top
      }
    })
}

export function buildFleetGraph(nodes, storedPositions = {}) {
  const positions = sanitizeFleetPositions(storedPositions)
  const idCounts = new Map()
  for (const node of nodes) {
    idCounts.set(node.stable_id, (idCounts.get(node.stable_id) ?? 0) + 1)
  }
  const ordered = nodes
    .filter(node => idCounts.get(node.stable_id) === 1)
    .map(node => ({ node, group: fleetGroupDescriptor(node) }))
    .sort((left, right) =>
      compareIds(left.group.id, right.group.id) || compareIds(left.node.stable_id, right.node.stable_id)
    )
  const defaultPositions = new Map()
  let groupOffsetX = 0
  for (const groupId of [...new Set(ordered.map(item => item.group.id))]) {
    const members = ordered.filter(item => item.group.id === groupId)
    const columns = Math.max(1, Math.ceil(Math.sqrt(members.length)))
    members.forEach((item, index) => {
      defaultPositions.set(item.node.stable_id, {
        x: groupOffsetX + (index % columns) * NODE_STEP_X,
        y: Math.floor(index / columns) * NODE_STEP_Y
      })
    })
    const groupWidth = NODE_WIDTH + Math.max(0, columns - 1) * NODE_STEP_X
    groupOffsetX += groupWidth + 160
  }
  const graphNodes = ordered.map(({ node, group }) => {
    const saved = positions[node.stable_id]
    const fallback = defaultPositions.get(node.stable_id)
    const observed = node.kind === 'observed'
    const status = observed
      ? { key: 'observed', label: 'OBSERVED · UNMANAGED', tone: 'info' }
      : statusFor(node)
    const nodeType = getFleetNodeType(node.node_type) ?? FLEET_NODE_TYPES.machine
    const x = saved?.x ?? fallback.x
    const y = saved?.y ?? fallback.y
    const searchValues = observed
      ? [
          node.naming.display_name,
          node.provider.label,
          node.provider.kind,
          node.provider.network_id,
          node.provider.instance_id,
          node.provider.node_id,
          node.observation.hostname,
          ...node.observation.addresses,
          ...node.observation.tags,
          status.label
        ]
      : [
          node.naming.display_name,
          node.naming.alias,
          node.naming.provider_name,
          node.identity.source,
          node.identity.network_id,
          node.identity.device_id,
          status.label,
          ...node.operations
        ]
    const searchText = normalizedSearch(searchValues.filter(Boolean).join(' '))
    return {
      id: node.stable_id,
      nodeType,
      label: node.naming.display_name,
      status,
      detail: observed ? node.provider.label : formatCanvasCapacity(node.readiness),
      ports: nodeType.defaultPorts,
      disabled: observed ? node.observation.expired : !node.managed.active,
      searchText,
      x,
      y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      group,
      source: node
    }
  })

  // fleet.desktop.v2 currently provides no relationship authority. The graph
  // engine supports edges, but the adapter must remain edge-free until a
  // versioned Fleet relationship contract supplies real evidence.
  return { nodes: graphNodes, edges: [], groups: buildFleetGroups(graphNodes) }
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
  return { nodes, edges, groups: buildFleetGroups(nodes) }
}

export function buildWorkflowGraph(workflow, storedPositions = {}) {
  const positions = sanitizeFleetPositions(storedPositions)
  const nodes = workflow.nodes.map(node => {
    const descriptor = getFleetNodeType(node.type)
    const position = positions[node.id] ?? node.position
    return {
      id: node.id,
      nodeType: descriptor,
      label: node.title,
      status: { key: 'unavailable', label: 'Editor only', tone: 'muted' },
      detail: descriptor.label,
      ports: descriptor.defaultPorts,
      disabled: descriptor.runtime === 'unavailable',
      searchText: normalizedSearch(`${node.title} ${descriptor.label} ${descriptor.category}`),
      x: position.x,
      y: position.y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      group: null,
      source: { kind: 'workflow', workflowNode: node }
    }
  })
  const nodeIds = new Set(nodes.map(node => node.id))
  const edges = workflow.connections
    .filter(edge => nodeIds.has(edge.source) && nodeIds.has(edge.target))
    .map(edge => ({ ...edge }))
  return { nodes, edges, groups: [] }
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
  const scale = Math.min(1.25, clampScale(
    Math.min(availableWidth / contentWidth, availableHeight / contentHeight)
  ))
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
  if (status.key === 'ready' || status.key === 'success') return 'var(--ui-green)'
  if (status.key === 'attention') return 'var(--ui-yellow)'
  if (status.key === 'inactive' || status.key === 'unavailable') return 'var(--ui-text-quaternary)'
  if (status.key === 'observed') return 'var(--fleet-observed)'
  if (status.key === 'execution') return 'var(--fleet-execution)'
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

function GraphGroups({ groups }) {
  return jsx('g', {
    'aria-hidden': true,
    children: groups.map(group =>
      jsxs('g', {
        children: [
          jsx('rect', {
            className: 'fleet-group-region',
            x: group.x,
            y: group.y,
            width: group.width,
            height: group.height,
            rx: 22,
            vectorEffect: 'non-scaling-stroke'
          }),
          jsx('foreignObject', {
            x: group.x + 14,
            y: group.y + 8,
            width: Math.max(1, group.width - 28),
            height: 28,
            style: { pointerEvents: 'none' },
            children: jsxs('div', {
              xmlns: 'http://www.w3.org/1999/xhtml',
              className: 'fleet-group-header flex h-full items-center gap-2',
              children: [
                jsx(Codicon, {
                  name: group.kind === 'observed' ? 'cloud' : 'server-environment',
                  size: '0.8rem'
                }),
                jsx('span', {
                  className: 'min-w-0 flex-1 truncate font-semibold',
                  children: shortLabel(group.label, 42)
                }),
                jsx('span', {
                  className: 'rounded-full bg-muted/60 px-2 py-0.5 text-[0.625rem] tabular-nums text-muted-foreground',
                  children: group.nodeIds.length
                })
              ]
            })
          })
        ]
      }, group.id)
    )
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

function FleetMiniMap({ graph, viewport, size, inspectorOpen = false }) {
  if (!graph.nodes.length) return null
  const regions = graph.groups.length ? graph.groups : graph.nodes
  const left = Math.min(...regions.map(region => region.x))
  const top = Math.min(...regions.map(region => region.y))
  const right = Math.max(...regions.map(region => region.x + region.width))
  const bottom = Math.max(...regions.map(region => region.y + region.height))
  const padding = 24
  const worldViewport = {
    x: -viewport.x / viewport.scale,
    y: -viewport.y / viewport.scale,
    width: size.width / viewport.scale,
    height: size.height / viewport.scale
  }
  const viewLeft = left - padding
  const viewTop = top - padding
  const viewRight = right + padding
  const viewBottom = bottom + padding
  const clippedViewport = {
    x: Math.max(viewLeft, worldViewport.x),
    y: Math.max(viewTop, worldViewport.y),
    width: Math.max(1, Math.min(viewRight, worldViewport.x + worldViewport.width) - Math.max(viewLeft, worldViewport.x)),
    height: Math.max(1, Math.min(viewBottom, worldViewport.y + worldViewport.height) - Math.max(viewTop, worldViewport.y))
  }
  return jsxs('svg', {
    className: 'fleet-minimap pointer-events-auto absolute bottom-3 right-3 z-10 h-24 w-40 rounded-xl border border-border/60 bg-background/80 p-1 shadow-lg backdrop-blur-sm',
    viewBox: `${left - padding} ${top - padding} ${Math.max(1, right - left + padding * 2)} ${Math.max(1, bottom - top + padding * 2)}`,
    role: 'img',
    'aria-label': 'Fleet minimap',
    'data-inspector-open': inspectorOpen,
    children: [
      jsx('g', {
        children: graph.groups.map(group =>
          jsx('rect', {
            x: group.x,
            y: group.y,
            width: group.width,
            height: group.height,
            rx: 14,
            fill: 'var(--ui-bg-sidebar)',
            fillOpacity: 0.54,
            stroke: 'var(--ui-stroke-secondary)',
            strokeWidth: 2
          }, group.id)
        )
      }),
      jsx('g', {
        children: graph.nodes.map(node =>
          jsx('rect', {
            x: node.x,
            y: node.y,
            width: node.width,
            height: node.height,
            rx: 8,
            fill: node.status.key === 'observed' ? 'var(--ui-text-secondary)' : statusColor(node.status),
            fillOpacity: 0.82,
            stroke: 'var(--ui-text-primary)',
            strokeOpacity: 0.28,
            strokeWidth: 1.5
          }, node.id)
        )
      }),
      jsx('rect', {
        className: 'fleet-minimap-viewport',
        x: clippedViewport.x,
        y: clippedViewport.y,
        width: clippedViewport.width,
        height: clippedViewport.height,
        rx: 6
      })
    ]
  })
}

function NodePort({ port, direction, index, total }) {
  if (!port || !FLEET_PORT_KINDS.includes(port.kind)) return null
  return jsx('span', {
    className: 'fleet-node-port',
    'data-direction': direction,
    'data-port-kind': port.kind,
    style: { top: `${((index + 1) / (total + 1)) * 100}%` },
    title: port.label ?? port.kind,
    'aria-hidden': true
  })
}

function NodeBadge({ badge }) {
  return jsx('span', {
    className: 'fleet-node-badge',
    'data-tone': badge.tone ?? 'neutral',
    children: badge.label
  })
}

function FleetCanvasNode({
  nodeType,
  category,
  icon,
  title,
  subtitle,
  status,
  badges = [],
  body,
  inputPorts = [],
  outputPorts = [],
  footer,
  selected,
  hovered,
  executionState = 'idle',
  disabled = false,
  showPorts = false
}) {
  return jsxs('div', {
    xmlns: 'http://www.w3.org/1999/xhtml',
    className: 'fleet-node-shell relative flex flex-col px-3.5 py-3',
    'data-node-type': nodeType,
    'data-category': category,
    'data-selected': selected,
    'data-hovered': hovered,
    'data-execution': executionState,
    'data-disabled': disabled,
    'data-show-ports': showPorts,
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-start gap-3',
        children: [
          jsx('div', {
            className: 'fleet-node-icon shrink-0',
            children: jsx(Codicon, {
              name: icon,
              size: '1rem',
              'aria-hidden': true
            })
          }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: 'truncate text-[0.8125rem] font-semibold leading-5 text-foreground',
                children: title
              }),
              jsx('div', {
                className: 'truncate text-[0.6875rem] leading-4 text-muted-foreground',
                children: subtitle
              })
            ]
          }),
          jsx('span', {
            className: 'mt-1 h-2 w-2 shrink-0 rounded-full',
            style: { background: statusColor(status) },
            title: status.label,
            'aria-hidden': true
          })
        ]
      }),
      jsx('div', {
        className: 'mt-2 flex min-w-0 items-center gap-1.5',
        children: badges.map(badge =>
          jsx(NodeBadge, { badge }, `${badge.label}:${badge.tone ?? 'neutral'}`)
        )
      }),
      jsx('div', {
        className: 'mt-auto truncate text-[0.6875rem] text-muted-foreground',
        children: body
      }),
      footer
        ? jsx('div', {
            className: 'mt-1 truncate text-[0.625rem] text-muted-foreground/75',
            children: footer
          })
        : null,
      ...inputPorts.map(port =>
        jsx(NodePort, {
          port,
          direction: 'input',
          index: inputPorts.indexOf(port),
          total: inputPorts.length
        }, `input:${port.id}`)
      ),
      ...outputPorts.map(port =>
        jsx(NodePort, {
          port,
          direction: 'output',
          index: outputPorts.indexOf(port),
          total: outputPorts.length
        }, `output:${port.id}`)
      )
    ]
  })
}

function MachineCanvasNode({ node, selected, hovered }) {
  const observed = node.source.kind === 'observed'
  const observation = observed ? node.source.observation : null
  const badges = observed
    ? [
        { label: 'Observed', tone: 'info' },
        { label: 'Unmanaged', tone: 'neutral' },
        ...(observation.expired ? [{ label: 'Expired', tone: 'attention' }] : [])
      ]
    : [{ label: node.status.label, tone: node.status.tone }]
  const body = observed
    ? observation.addresses[0] ?? 'No address observed'
    : node.detail
  const footer = observed
    ? `${observation.addresses.length} address${observation.addresses.length === 1 ? '' : 'es'} · provider evidence`
    : 'Managed Fleet node'
  return jsx(FleetCanvasNode, {
    nodeType: node.nodeType.id,
    category: node.nodeType.category,
    icon: node.nodeType.icon,
    title: node.label,
    subtitle: node.detail,
    status: node.status,
    badges,
    body,
    inputPorts: node.ports.filter(port => port.direction === 'input'),
    outputPorts: node.ports.filter(port => port.direction === 'output'),
    footer,
    selected,
    hovered,
    executionState: 'idle',
    disabled: node.disabled,
    showPorts: false
  })
}

function WorkflowCanvasNode({ node, selected, hovered }) {
  const workflowNode = node.source.workflowNode
  const descriptor = node.nodeType
  const categoryLabel = descriptor.category.replaceAll('-', ' ')
  const targetBadge = workflowNode.target
    ? [{ label: `${workflowNode.target.authority} target`, tone: 'info' }]
    : []
  return jsx(FleetCanvasNode, {
    nodeType: descriptor.id,
    category: descriptor.category,
    icon: descriptor.icon,
    title: node.label,
    subtitle: categoryLabel,
    status: node.status,
    badges: [{ label: 'Editor only', tone: 'neutral' }, ...targetBadge],
    body: descriptor.inputs.length || descriptor.outputs.length
      ? `${descriptor.inputs.length} input · ${descriptor.outputs.length} output`
      : 'Configuration node',
    inputPorts: descriptor.inputs,
    outputPorts: descriptor.outputs,
    footer: 'Execution unavailable',
    selected,
    hovered,
    executionState: 'idle',
    disabled: true,
    showPorts: true
  })
}

function CanvasNodeRenderer({ node, selected, hovered }) {
  return node.source.kind === 'workflow'
    ? jsx(WorkflowCanvasNode, { node, selected, hovered })
    : jsx(MachineCanvasNode, { node, selected, hovered })
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
  onPointerEnd,
  onPointerCancel
}) {
  const detailLabel = node.detail
  const identityLabel = node.source.kind === 'workflow'
    ? 'Workflow node'
    : node.source.kind === 'observed'
      ? 'Observed identity'
      : 'Stable identity'

  function focusSibling(event, offset, absolute = false) {
    const peers = [...event.currentTarget.parentElement.querySelectorAll('[data-fleet-node]')]
    const current = peers.indexOf(event.currentTarget)
    if (current < 0 || !peers.length) return
    const target = absolute
      ? peers[offset < 0 ? peers.length - 1 : 0]
      : peers[(current + offset + peers.length) % peers.length]
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
    role: 'button',
    tabIndex: selected ? 0 : -1,
    'aria-pressed': selected,
    'aria-label': `${node.label}, ${node.status.label}, ${detailLabel}, ${identityLabel} ${node.id}`,
    'data-fleet-node': node.id,
    className: animated ? 'fleet-node-enter' : undefined,
    onClick: event => {
      event.stopPropagation()
      event.currentTarget.focus({ preventScroll: true })
      onSelect(node.id)
    },
    onDoubleClick: event => {
      event.stopPropagation()
      onCenter(node.id)
    },
    onMouseEnter: () => onHover(node.id),
    onMouseLeave: () => onHover(null),
    onKeyDown,
    onPointerDown: event => {
      event.currentTarget.focus({ preventScroll: true })
      onPointerDown(event, node)
    },
    onPointerMove,
    onPointerUp: onPointerEnd,
    onPointerCancel,
    onLostPointerCapture: onPointerEnd,
    style: { cursor: 'grab', outline: 'none' },
    children: [
      jsx('title', {
        children: `${node.label} — ${node.status.label} — ${identityLabel} ${node.id}`
      }),
      jsx('rect', {
        width: node.width,
        height: node.height,
        rx: 14,
        fill: 'transparent',
        style: { pointerEvents: 'all' }
      }),
      jsx('foreignObject', {
        width: node.width,
        height: node.height,
        style: { overflow: 'visible', pointerEvents: 'none' },
        children: jsx(CanvasNodeRenderer, { node, selected, hovered })
      })
    ]
  })
}

function FleetCanvas({
  graph,
  positions,
  setPositions,
  commitPositions,
  selectedId,
  setSelectedId,
  animatedIds,
  inspectorOpen = false,
  canvasLabel = 'Fleet topology canvas. Arrow keys pan the canvas or move between focused nodes. Shift plus arrow moves a node. Plus and minus zoom; zero fits all.',
  emptyMessage = 'No machines match this view.'
}) {
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

  const zoomBy = useCallback(multiplier => {
    setViewport(current =>
      zoomFleetViewport(
        current,
        current.scale * multiplier,
        size.width / 2,
        size.height / 2
      )
    )
  }, [size])

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
      y: node.y,
      originalPositions: positions
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
    if (active.kind === 'node' && active.positions) commitPositions(active.positions)
  }

  function cancelPointer(event) {
    const active = pointerRef.current
    if (!active || active.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    pointerRef.current = null
    if (active.kind === 'node') setPositions(active.originalPositions)
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
    if (event.key === 'Escape' && pointerRef.current) {
      event.preventDefault()
      const active = pointerRef.current
      pointerRef.current = null
      if (active.kind === 'node') setPositions(active.originalPositions)
      return
    }
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
    } else if ((event.key === 'Enter' || event.key === ' ') && graph.nodes.length) {
      event.preventDefault()
      rootRef.current?.querySelector('[data-fleet-node]')?.focus({ preventScroll: true })
    }
  }

  return jsxs('div', {
    className: 'fleet-canvas-surface relative min-h-0 flex-1 overflow-hidden rounded-2xl',
    children: [
      jsxs('div', {
        className: 'absolute right-3 top-3 z-10 flex gap-1 rounded-xl border border-border/60 bg-background/85 p-1 shadow-lg backdrop-blur-sm',
        children: [
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'ghost',
            onClick: () => zoomBy(1 / 1.2),
            title: 'Zoom out',
            'aria-label': 'Zoom out',
            children: jsx(Codicon, { name: 'zoom-out', size: '0.85rem' })
          }),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'ghost',
            onClick: () => zoomBy(1.2),
            title: 'Zoom in',
            'aria-label': 'Zoom in',
            children: jsx(Codicon, { name: 'zoom-in', size: '0.85rem' })
          }),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'ghost',
            onClick: fitAll,
            disabled: !graph.nodes.length,
            title: 'Fit all nodes',
            'aria-label': 'Fit all nodes',
            children: jsx(Codicon, { name: 'screen-full', size: '0.85rem' })
          }),
          jsx(Button, {
            type: 'button',
            size: 'sm',
            variant: 'ghost',
            onClick: () => centerNode(selectedId),
            disabled: !selectedId || !nodeById.has(selectedId),
            title: 'Center selected node',
            'aria-label': 'Center selected node',
            children: jsx(Codicon, { name: 'target', size: '0.85rem' })
          })
        ]
      }),
      jsx('svg', {
        ref: rootRef,
        className: 'fleet-canvas-grid h-full min-h-80 w-full select-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        role: 'region',
        tabIndex: selectedId ? -1 : 0,
        'aria-label': canvasLabel,
        style: { touchAction: 'none' },
        onPointerDown: beginPan,
        onPointerMove: movePointer,
        onPointerUp: endPointer,
        onPointerCancel: cancelPointer,
        onLostPointerCapture: endPointer,
        onWheel,
        onKeyDown: onCanvasKeyDown,
        children: jsx('g', {
          transform: `translate(${viewport.x} ${viewport.y}) scale(${viewport.scale})`,
          children: [
            jsx(GraphGroups, { groups: graph.groups }),
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
                  onPointerEnd: endPointer,
                  onPointerCancel: cancelPointer
                }, node.id)
              )
            })
          ]
        })
      }),
      jsx(FleetMiniMap, { graph, viewport, size, inspectorOpen }),
      !graph.nodes.length
        ? jsxs('div', {
            className: 'pointer-events-none absolute inset-0 grid place-items-center text-muted-foreground',
            children: jsx('div', {
              className: 'grid justify-items-center gap-2',
              children: [
                jsx(Codicon, { name: 'search-stop', size: '1.15rem' }),
                jsx('span', { className: 'text-sm', children: emptyMessage })
              ]
            })
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
        style: { overflowWrap: 'anywhere', wordBreak: 'break-word' },
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

function observedValue(value) {
  if (value == null) return 'Unknown'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return String(value)
}

function ObservedNodeInspector({ node }) {
  const observation = node.observation
  const freshness = formatFleetAge(
    Math.max(0, Date.now() - Date.parse(observation.last_observed_at))
  )
  return jsxs('aside', {
    className: 'h-full min-h-0 w-full shrink-0 overflow-auto px-5 py-4',
    'aria-label': `Observed node inspector for ${node.naming.display_name}`,
    children: [
      jsx('p', {
        className: 'mb-4 rounded-xl bg-muted/25 p-3 text-xs leading-5 text-muted-foreground',
        children: 'Provider observation only. This machine is visible, but it is not admitted, trusted, ready, schedulable, or executable by Fleet.'
      }),
      jsxs('div', {
        className: 'grid gap-5',
        children: [
          jsx(InspectorSection, {
            title: 'Overview',
            children: jsxs('dl', {
              className: 'grid gap-2.5',
              children: [
                jsx(InspectorRow, { label: 'Online', value: observedValue(observation.online) }),
                jsx(InspectorRow, {
                  label: 'Addresses',
                  value: observation.addresses.length
                    ? observation.addresses.join(', ')
                    : 'None observed',
                  mono: true
                }),
                jsx(InspectorRow, {
                  label: 'Last seen',
                  value: observedValue(observation.last_seen_at)
                }),
                jsx(InspectorRow, { label: 'Last observed', value: observation.last_observed_at }),
                jsx(InspectorRow, { label: 'Freshness', value: freshness }),
                jsx(InspectorRow, {
                  label: 'Tags',
                  value: observation.tags.length
                    ? observation.tags.join(', ')
                    : 'None observed',
                  mono: true
                })
              ]
            })
          }),
          jsx(InspectorSection, {
            title: 'Fleet',
            children: jsxs('dl', {
              className: 'grid gap-2.5',
              children: [
                jsx(InspectorRow, { label: 'Management', value: 'Unmanaged' }),
                jsx(InspectorRow, {
                  label: 'Readiness',
                  value: 'Not applicable until managed'
                }),
                jsx(InspectorRow, { label: 'Authority', value: 'None' }),
                jsx(InspectorRow, {
                  label: 'Classification',
                  value: observation.classification.replaceAll('_', ' ')
                })
              ]
            })
          }),
          jsx('details', {
            className: 'border-t border-border/60 pt-4',
            children: [
              jsx('summary', {
                className: 'cursor-pointer text-xs font-semibold text-foreground',
                children: 'Technical details'
              }),
              jsxs('dl', {
                className: 'mt-3 grid gap-2.5',
                children: [
                  jsx(InspectorRow, { label: 'Provider', value: node.provider.label }),
                  jsx(InspectorRow, {
                    label: 'Provider FQDN',
                    value: node.naming.technical_name,
                    mono: true
                  }),
                  jsx(InspectorRow, {
                    label: 'Provider node',
                    value: node.provider.node_id,
                    mono: true
                  }),
                  jsx(InspectorRow, {
                    label: 'Network',
                    value: node.provider.network_id,
                    mono: true
                  }),
                  jsx(InspectorRow, {
                    label: 'Instance',
                    value: node.provider.instance_id,
                    mono: true
                  }),
                  jsx(InspectorRow, {
                    label: 'Observed ID',
                    value: observation.observed_id,
                    mono: true
                  }),
                  jsx(InspectorRow, {
                    label: 'Stable fingerprint',
                    value: observation.observed_id,
                    mono: true
                  }),
                  jsx(InspectorRow, {
                    label: 'First observed',
                    value: observation.first_observed_at
                  }),
                  jsx(InspectorRow, { label: 'Snapshot', value: observation.snapshot_at }),
                  jsx(InspectorRow, {
                    label: 'Provider expired',
                    value: observedValue(observation.expired)
                  })
                ]
              })
            ]
          })
        ]
      })
    ]
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
    setMutation({ state: 'pending', message: 'Clearing local alias…' })
    try {
      await ctx.rest(`/nodes/${encodeURIComponent(node.stable_id)}/alias`, {
        method: 'DELETE',
        body: aliasClearMutationBody(node)
      })
      await reconcile()
      setMutation({
        state: 'success',
        message: node.naming.provider_name
          ? 'Provider name restored.'
          : 'Alias cleared; stable device ID is now displayed.'
      })
    } catch {
      try {
        await reconcile()
      } catch {}
      setMutation({ state: 'error', message: 'Local alias could not be cleared.' })
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
    className: 'h-full min-h-0 w-full shrink-0 overflow-auto rounded-lg border border-border bg-background p-4 lg:w-96',
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
            children: jsxs('div', {
              className: 'grid gap-2',
              children: [
                jsxs('dl', {
                  className: 'grid gap-2',
                  children: [
                    jsx(InspectorRow, { label: 'Source', value: node.identity.source, mono: true }),
                    jsx(InspectorRow, { label: 'Network', value: node.identity.network_id, mono: true }),
                    jsx(InspectorRow, { label: 'Device', value: node.identity.device_id, mono: true }),
                    jsx(InspectorRow, { label: 'Stable ID', value: node.stable_id, mono: true }),
                    jsx(InspectorRow, { label: 'Binding', value: node.managed.binding_generation, mono: true })
                  ]
                }),
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
                    : `Provider name unavailable; clearing the alias uses stable device ID ${providerFallback}.`
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
                      children: 'Clear local alias'
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
  ['observed', 'Observed'],
  ['ready', 'Ready'],
  ['attention', 'Attention'],
  ['awaiting', 'Awaiting evidence'],
  ['inactive', 'Inactive']
]

function FleetInspectorDrawer({ node, ctx, refresh, onClose }) {
  const closeRef = useRef(null)
  const returnFocusRef = useRef(null)

  useEffect(() => {
    returnFocusRef.current = document.activeElement
    const frame = requestAnimationFrame(() => closeRef.current?.focus())
    const onKeyDown = event => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      cancelAnimationFrame(frame)
      document.removeEventListener('keydown', onKeyDown)
      requestAnimationFrame(() => returnFocusRef.current?.focus?.())
    }
  }, [onClose])

  const observed = node.kind === 'observed'
  return jsxs('section', {
    className: 'fleet-inspector-drawer absolute inset-y-3 right-3 z-20 flex min-h-0 flex-col overflow-hidden rounded-2xl',
    role: 'dialog',
    'aria-modal': false,
    'aria-label': 'Fleet inspector drawer',
    onPointerDown: event => event.stopPropagation(),
    onWheel: event => event.stopPropagation(),
    children: [
      jsxs('header', {
        className: 'flex items-start gap-3 border-b border-border/60 px-5 py-4',
        children: [
          jsx('div', {
            className: 'fleet-node-icon shrink-0',
            children: jsx(Codicon, {
              name: getFleetNodeType(node.node_type)?.icon ?? 'symbol-misc',
              size: '1rem',
              'aria-hidden': true
            })
          }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: 'truncate text-sm font-semibold text-foreground',
                children: node.naming.display_name
              }),
              jsx('div', {
                className: 'mt-0.5 truncate text-xs text-muted-foreground',
                children: observed ? node.provider.label : 'Managed Fleet machine'
              }),
              jsx('div', {
                className: 'mt-2 flex flex-wrap gap-1.5',
                children: observed
                  ? [
                      jsx(NodeBadge, {
                        badge: { label: node.provider.label, tone: 'neutral' }
                      }, 'provider'),
                      jsx(NodeBadge, {
                        badge: { label: 'Observed', tone: 'info' }
                      }, 'observed'),
                      jsx(NodeBadge, {
                        badge: { label: 'Unmanaged', tone: 'neutral' }
                      }, 'unmanaged')
                    ]
                  : [
                      jsx(NodeBadge, {
                        badge: { label: 'Managed', tone: 'neutral' }
                      }, 'managed')
                    ]
              })
            ]
          }),
          jsx(Button, {
            ref: closeRef,
            type: 'button',
            size: 'sm',
            variant: 'ghost',
            onClick: onClose,
            title: 'Close inspector',
            'aria-label': 'Close node inspector',
            children: jsx(Codicon, { name: 'close', size: '0.8rem' })
          })
        ]
      }),
      jsx('div', {
        className: 'min-h-0 flex-1 overflow-hidden',
        children: observed
          ? jsx(ObservedNodeInspector, { node })
          : jsx(NodeInspector, { node, ctx, refresh })
      })
    ]
  })
}

const CATEGORY_LABELS = Object.freeze({
  machine: 'Machines',
  trigger: 'Triggers',
  'fleet-action': 'Fleet actions',
  'hermes-action': 'Hermes actions',
  'flow-control': 'Flow',
  condition: 'Conditions',
  data: 'Data',
  integration: 'Integrations',
  'human-approval': 'Human'
})

function WorkflowPalette({ query, onQuery, onAdd, atLimit }) {
  const descriptors = [...createFleetNodeRegistry().values()]
    .filter(descriptor => descriptor.id !== 'machine')
    .filter(descriptor => {
      const search = normalizedSearch(query)
      return !search || normalizedSearch(
        `${descriptor.label} ${CATEGORY_LABELS[descriptor.category]} ${descriptor.id}`
      ).includes(search)
    })
  const grouped = FLEET_NODE_TYPE_CATEGORIES
    .map(category => [category, descriptors.filter(item => item.category === category)])
    .filter(([, items]) => items.length)

  return jsxs('aside', {
    className: 'fleet-workflow-palette flex min-h-0 w-64 shrink-0 flex-col',
    'aria-label': 'Workflow node palette',
    children: [
      jsxs('div', {
        className: 'border-b border-border/60 px-3 py-3',
        children: [
          jsx('div', {
            className: 'text-xs font-semibold text-foreground',
            children: 'Node palette'
          }),
          jsx('div', {
            className: 'mt-0.5 text-[0.6875rem] text-muted-foreground',
            children: 'Editor types · runtime coming later'
          }),
          jsx(SearchField, {
            value: query,
            onChange: onQuery,
            placeholder: 'Search node types',
            'aria-label': 'Search workflow node types',
            containerClassName: 'mt-2 w-full'
          })
        ]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsx('div', {
          className: 'grid gap-4 p-3',
          children: grouped.map(([category, items]) =>
            jsxs('section', {
              children: [
                jsx('h3', {
                  className: 'mb-1.5 text-[0.625rem] font-semibold uppercase tracking-[0.14em] text-muted-foreground',
                  children: CATEGORY_LABELS[category]
                }),
                jsx('div', {
                  className: 'grid gap-1',
                  children: items.map(descriptor =>
                    jsxs('button', {
                      type: 'button',
                      disabled: atLimit,
                      'aria-disabled': atLimit,
                      className: 'group flex min-h-10 items-center gap-2 rounded-lg px-2 text-left text-xs text-foreground transition-colors hover:bg-muted/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
                      onClick: () => onAdd(descriptor.id),
                      title: atLimit
                        ? 'Workflow node limit reached (256)'
                        : `Add ${descriptor.label} editor node`,
                      children: [
                        jsx('span', {
                          className: 'fleet-node-icon !h-7 !w-7 shrink-0 !rounded-md',
                          children: jsx(Codicon, {
                            name: descriptor.icon,
                            size: '0.8rem',
                            'aria-hidden': true
                          })
                        }),
                        jsxs('span', {
                          className: 'min-w-0 flex-1',
                          children: [
                            jsx('span', {
                              className: 'block truncate font-medium',
                              children: descriptor.label
                            }),
                            jsx('span', {
                              className: 'block text-[0.625rem] text-muted-foreground',
                              children: 'Coming later'
                            })
                          ]
                        }),
                        jsx(Codicon, {
                          name: 'add',
                          size: '0.75rem',
                          className: 'text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100'
                        })
                      ]
                    }, descriptor.id)
                  )
                })
              ]
            }, category)
          )
        })
      })
    ]
  })
}

function WorkflowInspectorDrawer({ node, onClose }) {
  const descriptor = getFleetNodeType(node.type)
  const closeRef = useRef(null)
  const returnFocusRef = useRef(null)
  useEffect(() => {
    returnFocusRef.current = document.activeElement
    requestAnimationFrame(() => closeRef.current?.focus())
    const onKeyDown = event => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      requestAnimationFrame(() => returnFocusRef.current?.focus?.())
    }
  }, [onClose])

  return jsxs('section', {
    className: 'fleet-inspector-drawer absolute inset-y-3 right-3 z-20 flex min-h-0 flex-col overflow-hidden rounded-2xl',
    role: 'dialog',
    'aria-modal': false,
    'aria-label': 'Workflow node inspector',
    onPointerDown: event => event.stopPropagation(),
    children: [
      jsxs('header', {
        className: 'flex items-start gap-3 border-b border-border/60 px-5 py-4',
        children: [
          jsx('span', {
            className: 'fleet-node-icon shrink-0',
            children: jsx(Codicon, { name: descriptor.icon, size: '1rem' })
          }),
          jsxs('div', {
            className: 'min-w-0 flex-1',
            children: [
              jsx('div', {
                className: 'truncate text-sm font-semibold text-foreground',
                children: node.title
              }),
              jsx('div', {
                className: 'mt-0.5 text-xs text-muted-foreground',
                children: `${descriptor.label} · ${CATEGORY_LABELS[descriptor.category]}`
              }),
              jsx('div', {
                className: 'mt-2 flex flex-wrap gap-1.5',
                children: [
                  jsx(NodeBadge, {
                    badge: { label: 'Editor only', tone: 'neutral' }
                  }, 'editor'),
                  jsx(NodeBadge, {
                    badge: { label: 'Execution unavailable', tone: 'neutral' }
                  }, 'runtime')
                ]
              })
            ]
          }),
          jsx(Button, {
            ref: closeRef,
            type: 'button',
            size: 'sm',
            variant: 'ghost',
            onClick: onClose,
            'aria-label': 'Close workflow node inspector',
            children: jsx(Codicon, { name: 'close', size: '0.8rem' })
          })
        ]
      }),
      jsx(ScrollArea, {
        className: 'min-h-0 flex-1',
        children: jsxs('div', {
          className: 'grid gap-5 px-5 py-4 text-xs',
          children: [
            jsxs('section', {
              children: [
                jsx('h3', {
                  className: 'mb-2 font-semibold text-foreground',
                  children: 'Ports'
                }),
                jsx('p', {
                  className: 'text-muted-foreground',
                  children: `${descriptor.inputs.length} typed input · ${descriptor.outputs.length} typed output`
                })
              ]
            }),
            node.target
              ? jsxs('section', {
                  children: [
                    jsx('h3', {
                      className: 'mb-2 font-semibold text-foreground',
                      children: 'Machine target'
                    }),
                    jsx('p', {
                      className: 'text-muted-foreground',
                      children: `${node.target.authority} reference · no execution authority`
                    })
                  ]
                })
              : null,
            jsxs('section', {
              className: 'rounded-xl bg-muted/25 p-3',
              children: [
                jsx('div', {
                  className: 'font-medium text-foreground',
                  children: 'Foundation status'
                }),
                jsx('p', {
                  className: 'mt-1 leading-5 text-muted-foreground',
                  children: 'This node is a serializable editor descriptor. No remote action, scheduling, reservation, or workflow execution is available in this milestone.'
                })
              ]
            })
          ]
        })
      })
    ]
  })
}

function WorkflowModePanel({ history, setHistory }) {
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [editorNotice, setEditorNotice] = useState(null)
  const [positions, setPositions] = useState(() => Object.fromEntries(
    history.present.nodes.map(node => [node.id, node.position])
  ))
  const clipboardRef = useRef(null)
  const counterRef = useRef(history.present.nodes.length + 1)
  const workflow = history.present
  const graph = useMemo(
    () => buildWorkflowGraph(workflow, positions),
    [positions, workflow]
  )
  const selectedNode = workflow.nodes.find(node => node.id === selectedId) ?? null
  const atLimit = workflow.nodes.length >= WORKFLOW_LIMIT_COUNT
  const closeInspector = useCallback(() => setInspectorOpen(false), [])
  const selectNode = useCallback(id => {
    setSelectedId(id)
    setInspectorOpen(Boolean(id))
    setEditorNotice(null)
  }, [])

  useEffect(() => {
    setPositions(Object.fromEntries(
      history.present.nodes.map(node => [node.id, node.position])
    ))
    if (selectedId && !history.present.nodes.some(node => node.id === selectedId)) {
      setSelectedId(null)
      setInspectorOpen(false)
    }
  }, [history.present, selectedId])

  const applyEdit = useCallback(next => {
    setHistory(current => applyWorkflowEdit(current, next))
  }, [setHistory])

  const addNode = useCallback(type => {
    if (atLimit) {
      setEditorNotice('Workflow node limit reached (256).')
      return
    }
    const index = workflow.nodes.length
    const id = `${type}-${Date.now().toString(36)}-${counterRef.current++}`
    const position = {
      x: 64 + (index % 3) * NODE_STEP_X,
      y: 64 + Math.floor(index / 3) * NODE_STEP_Y
    }
    try {
      const next = addWorkflowNode(workflow, { id, type, position })
      setPositions(current => ({ ...current, [id]: position }))
      applyEdit(next)
      selectNode(id)
    } catch (error) {
      setEditorNotice(error instanceof Error ? error.message : 'Unable to add workflow node.')
    }
  }, [applyEdit, atLimit, selectNode, workflow])

  const commitPositions = useCallback(value => {
    const clean = sanitizeFleetPositions(value)
    const next = {
      ...workflow,
      nodes: workflow.nodes.map(node => ({
        ...node,
        position: clean[node.id] ?? node.position
      }))
    }
    applyEdit(next)
  }, [applyEdit, workflow])

  const deleteSelected = useCallback(() => {
    if (!selectedId) return
    applyEdit(deleteWorkflowSelection(workflow, [selectedId]))
    setSelectedId(null)
    setInspectorOpen(false)
  }, [applyEdit, selectedId, workflow])

  const duplicateSelected = useCallback(() => {
    if (!selectedId) return
    if (atLimit) {
      setEditorNotice('Workflow node limit reached (256).')
      return
    }
    const prefix = `copy-${Date.now().toString(36)}`
    try {
      const next = duplicateWorkflowSelection(workflow, [selectedId], { idPrefix: prefix })
      applyEdit(next)
      selectNode(next.nodes.at(-1)?.id ?? null)
    } catch (error) {
      setEditorNotice(error instanceof Error ? error.message : 'Unable to duplicate workflow node.')
    }
  }, [applyEdit, atLimit, selectNode, selectedId, workflow])

  const copySelected = useCallback(() => {
    if (selectedId) clipboardRef.current = copyWorkflowSelection(workflow, [selectedId])
  }, [selectedId, workflow])

  const paste = useCallback(() => {
    if (!clipboardRef.current) return
    if (atLimit) {
      setEditorNotice('Workflow node limit reached (256).')
      return
    }
    const prefix = `paste-${Date.now().toString(36)}`
    try {
      const next = pasteWorkflowClipboard(workflow, clipboardRef.current, { idPrefix: prefix })
      applyEdit(next)
      selectNode(next.nodes.at(-1)?.id ?? null)
    } catch (error) {
      setEditorNotice(error instanceof Error ? error.message : 'Unable to paste workflow nodes.')
    }
  }, [applyEdit, atLimit, selectNode, workflow])

  function onKeyDown(event) {
    if (event.target.closest?.('input, textarea, [contenteditable=true]')) return
    const mod = event.metaKey || event.ctrlKey
    if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault()
      deleteSelected()
    } else if (mod && event.key.toLowerCase() === 'd') {
      event.preventDefault()
      duplicateSelected()
    } else if (mod && event.key.toLowerCase() === 'c') {
      event.preventDefault()
      copySelected()
    } else if (mod && event.key.toLowerCase() === 'v') {
      event.preventDefault()
      paste()
    } else if (mod && event.key.toLowerCase() === 'z') {
      event.preventDefault()
      setHistory(current => event.shiftKey ? redoWorkflow(current) : undoWorkflow(current))
    }
  }

  const canvas = jsxs('div', {
    className: 'relative flex min-h-0 min-w-0 flex-1 overflow-hidden',
    onKeyDown,
    children: [
      jsx(FleetCanvas, {
        graph,
        positions,
        setPositions,
        commitPositions,
        selectedId,
        setSelectedId: selectNode,
        animatedIds: new Set(),
        inspectorOpen: Boolean(selectedNode && inspectorOpen),
        canvasLabel: 'Workflow editor canvas. Arrow keys pan the canvas or move between focused nodes. Shift plus arrow moves a node. Plus and minus zoom; zero fits all.',
        emptyMessage: 'Add a node from the palette to begin.'
      }),
      selectedNode && inspectorOpen
        ? jsx(WorkflowInspectorDrawer, {
            node: selectedNode,
            onClose: closeInspector
          })
        : null
    ]
  })

  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-3',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1.5 px-4',
        children: [
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'ghost',
            onClick: () => setHistory(current => undoWorkflow(current)),
            disabled: !history.past.length,
            title: 'Undo (Ctrl+Z)',
            children: jsx(Codicon, { name: 'discard', size: '0.8rem' })
          }),
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'ghost',
            onClick: () => setHistory(current => redoWorkflow(current)),
            disabled: !history.future.length,
            title: 'Redo (Ctrl+Shift+Z)',
            children: jsx(Codicon, { name: 'redo', size: '0.8rem' })
          }),
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'ghost',
            onClick: duplicateSelected,
            disabled: !selectedId || atLimit,
            children: 'Duplicate'
          }),
          jsx(Button, {
            type: 'button', size: 'sm', variant: 'ghost',
            onClick: deleteSelected,
            disabled: !selectedId,
            children: 'Delete'
          }),
          editorNotice
            ? jsx('span', {
                className: 'ml-auto text-[0.6875rem] text-muted-foreground',
                role: 'status',
                'aria-live': 'polite',
                children: editorNotice
              })
            : jsx('span', {
                className: 'ml-auto text-[0.6875rem] text-muted-foreground',
                children: `${workflow.nodes.length} editor node${workflow.nodes.length === 1 ? '' : 's'} · execution unavailable`
              })
        ]
      }),
      jsxs('div', {
        className: 'mx-4 flex min-h-0 flex-1 overflow-hidden rounded-xl border border-border/60',
        children: [
          jsx(WorkflowPalette, { query, onQuery: setQuery, onAdd: addNode, atLimit }),
          jsx(ContextMenu, {
            children: [
              jsx(ContextMenuTrigger, { asChild: true, children: canvas }),
              jsxs(ContextMenuContent, {
                'aria-label': 'Workflow canvas actions',
                children: [
                  jsx(ContextMenuItem, {
                    onSelect: () => addNode('manual-trigger'),
                    disabled: atLimit,
                    children: 'Add manual trigger'
                  }),
                  jsx(ContextMenuSeparator, {}),
                  jsx(ContextMenuItem, {
                    onSelect: duplicateSelected,
                    disabled: !selectedId || atLimit,
                    children: 'Duplicate selected'
                  }),
                  jsx(ContextMenuItem, {
                    onSelect: deleteSelected,
                    disabled: !selectedId,
                    children: 'Delete selected'
                  })
                ]
              })
            ]
          })
        ]
      }),
      jsx('div', {
        className: 'px-4 pb-1 text-[0.6875rem] text-muted-foreground',
        children: 'Editor foundation only · typed connections serialize but no workflow execution runtime is present'
      })
    ]
  })
}

function FleetCanvasWorkspace({ overview, ctx, refresh, activity }) {
  const [mode, setMode] = useState('topology')
  const [workflowHistory, setWorkflowHistory] = useState(() =>
    createWorkflowHistory(createEmptyWorkflow('local-workflow'))
  )
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [positions, setPositions] = useState(() =>
    sanitizeFleetPositions(ctx.storage.get(LAYOUT_STORAGE_KEY, {}))
  )
  const canvasNodes = useMemo(
    () => buildFleetCanvasNodes(overview),
    [overview.nodes, overview.observed_nodes]
  )
  const [selectedId, setSelectedId] = useState(null)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const positionsRef = useRef(positions)
  positionsRef.current = positions

  const graph = useMemo(
    () => buildFleetGraph(canvasNodes, positions),
    [canvasNodes, positions]
  )
  const visibleGraph = useMemo(
    () => filterFleetGraph(graph, query, filter),
    [filter, graph, query]
  )
  const selectedNode = canvasNodes.find(node => node.stable_id === selectedId) ?? null
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
      setSelectedId(null)
      setInspectorOpen(false)
    } else if (
      selectedId &&
      visibleGraph.nodes.length &&
      !visibleGraph.nodes.some(node => node.id === selectedId)
    ) {
      setSelectedId(null)
      setInspectorOpen(false)
    }
  }, [graph.nodes, selectedId, visibleGraph.nodes])

  const closeInspector = useCallback(() => setInspectorOpen(false), [])
  const selectNode = useCallback(id => {
    setSelectedId(id)
    setInspectorOpen(Boolean(id))
  }, [])
  const createFromSelection = useCallback(() => {
    if (!selectedNode) return
    setWorkflowHistory(current => {
      const workflow = appendTopologyTargetsToWorkflow(current.present, [selectedNode])
      if (workflow === current.present) return current
      return applyWorkflowEdit(current, workflow)
    })
    setMode('workflow')
  }, [selectedNode])

  const topologyPanel = jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-3 px-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-1.5',
        children: [
          jsx(SearchField, {
            value: query,
            onChange: setQuery,
            placeholder: 'Search machines',
            'aria-label': 'Search nodes and observations',
            containerClassName: 'mr-2 min-w-52'
          }),
          ...FILTERS.map(([key, label]) =>
            jsx(Button, {
              type: 'button',
              size: 'sm',
              variant: filter === key ? 'secondary' : 'ghost',
              'aria-pressed': filter === key,
              onClick: () => setFilter(key),
              children: label
            }, key)
          ),
          selectedNode
            ? jsx(Button, {
                type: 'button',
                size: 'sm',
                variant: 'outline',
                className: 'ml-auto',
                onClick: createFromSelection,
                disabled: workflowHistory.present.nodes.length >= WORKFLOW_LIMIT_COUNT,
                title: workflowHistory.present.nodes.length >= WORKFLOW_LIMIT_COUNT
                  ? 'Workflow node limit reached (256)'
                  : 'Create or update the local workflow from this machine',
                children: 'Create workflow from selection'
              })
            : null
        ]
      }),
      jsxs('div', {
        className: 'relative flex min-h-0 flex-1 overflow-hidden',
        children: [
          jsx(FleetCanvas, {
            graph: visibleGraph,
            positions,
            setPositions,
            commitPositions,
            selectedId,
            setSelectedId: selectNode,
            animatedIds,
            inspectorOpen: Boolean(selectedNode && inspectorOpen)
          }),
          selectedNode && inspectorOpen
            ? jsx(FleetInspectorDrawer, {
                node: selectedNode,
                ctx,
                refresh,
                onClose: closeInspector
              })
            : null
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-x-4 gap-y-2 text-[0.6875rem] text-muted-foreground',
        children: [
          jsxs('span', {
            className: 'inline-flex items-center gap-1.5',
            children: [jsx(StatusDot, { tone: 'muted' }), 'Observed · unmanaged']
          }),
          jsx('span', { children: 'Wheel zoom · drag pan · 0 fit' }),
          jsx('span', { children: 'Layout saved locally' }),
          jsx('span', { children: '0 relationship edges' })
        ]
      })
    ]
  })

  return jsxs('div', {
    className: 'fleet-canvas-root flex min-h-0 flex-1 flex-col gap-3 py-4',
    children: [
      jsx('style', { children: FLEET_CANVAS_STYLES }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-3 px-4',
        children: [
          jsx(SegmentedControl, {
            options: [
              { id: 'topology', label: 'Topology' },
              { id: 'workflow', label: 'Workflow' }
            ],
            value: mode,
            onChange: setMode
          }),
          jsx('span', {
            className: 'text-[0.6875rem] text-muted-foreground',
            children: mode === 'topology'
              ? 'Live provider topology'
              : 'Editor foundation · execution unavailable'
          }),
          null
        ]
      }),
      mode === 'workflow'
        ? jsx(WorkflowModePanel, {
            history: workflowHistory,
            setHistory: setWorkflowHistory
          })
        : topologyPanel
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
    children: [
      jsx(StatusDot, { tone: query.data.summary.not_ready ? 'warn' : 'good' }),
      `Fleet ${query.data.summary.ready}/${query.data.summary.managed} ready · ${query.data.summary.observed_unmanaged} observed`
    ]
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
  const activityState = useFleetActivity(query.data)

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
                children: 'Managed Fleet authority and distinct provider observations on a stable operator layout.'
              })
            ]
          }),
          jsxs('div', {
            className: 'flex min-w-0 flex-wrap items-start justify-end gap-4',
            children: [
              jsx(SummaryItem, { label: 'Managed', value: overview.summary.managed }),
              jsx(SummaryItem, { label: 'Observed', value: overview.summary.observed_unmanaged }),
              jsx(SummaryItem, { label: 'Ready', value: overview.summary.ready }),
              jsx(SummaryItem, { label: 'Attention', value: overview.summary.not_ready }),
              jsx(ConnectionChip, { state: events.connection }),
              jsx(Button, {
                type: 'button',
                size: 'sm',
                variant: activityOpen ? 'secondary' : 'outline',
                'aria-expanded': activityOpen,
                onClick: () => setActivityOpen(value => !value),
                children: `Activity (${activityState.activity.length})`
              })
            ]
          })
        ]
      }),
      activityOpen ? jsx(ActivityDrawer, { activity: activityState.activity, onClear: activityState.clearActivity }) : null,
      jsx(FleetCanvasWorkspace, { overview, ctx, refresh: query.refetch, activity: activityState.activity })
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
