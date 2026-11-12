// DwarfStar settings page: the ds4 counterpart of Settings → Providers → Local Models.
// Parts of its wording and layout come from Hermes Desktop's Local Models page
// (Hermes Agent, MIT, © Nous Research; notice in the NOTICE file).
// It follows that page row for row (setup hero with its stage rail, Local runtime, This machine,
// Models, Find more models), with the same components, wording and toasts. It differs only where ds4
// does: no Hugging Face search (ds4 runs only its own GGUFs), no per-GPU memory figures, and a model
// that streams its experts from the SSD instead of "spilling to RAM".
// Plain ESM loaded uncompiled by Hermes Desktop; talks to dashboard/plugin_api.py through ctx.rest.
import {
  Badge, Button, EmptyState, ErrorState, GlyphSpinner, ListRow, Tip,
  gatewayActivationEpoch, host, icons, useGatewayActivationEpoch,
  useMutation, useQuery, useQueryClient, useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

// Wording is Local Models' own (apps/desktop/src/i18n/en_local_models.ts) wherever it applies.
const copy = {
  runtimeTitle: 'Local runtime',
  serverRunning: 'Running',
  runtimeReady: backend => `Ready · ${backend}`,
  runtimeInstalled: 'ds4 runtime installed',
  runtimeInstalledDetail: (tag, backend) => `Build ${tag}, ${backend} backend. Hermes starts and manages the server for you.`,
  runtimeRunningDetail: 'The local server is running. Turning it off frees all GPU memory and stops new chats from using local models until you turn it back on.',
  installTitle: 'Install the local runtime',
  installDetail: prebuilt => (prebuilt
    ? 'Downloads the ds4 inference engine (a few MB). Models you download run entirely on this machine — no account, nothing leaves your computer.'
    : 'Builds the ds4 inference engine from its source (about a minute; needs Apple\'s Command Line Tools). Models you download run entirely on this machine — no account, nothing leaves your computer.'),
  installAction: 'Install runtime',
  installing: 'Installing runtime…',
  installFailed: 'Runtime install failed',
  installDoneToast: 'Local runtime installed and ready.',
  upToDateTitle: 'Engine up to date',
  upToDateDetail: (tag, backend) => `Running ds4 ${tag} (${backend}).`,
  hardwareTitle: 'This machine',
  ram: label => `${label} RAM`,
  unifiedMemory: 'Unified memory',
  modelsTitle: 'Models',
  recommended: 'Recommended',
  recommendedReason: 'The model ds4\'s hardware guide suggests for this machine\'s memory.',
  noRecommendationTitle: 'No automatic recommendation for this machine',
  noRecommendationDetail: 'ds4\'s hardware guide suggests DeepSeek V4 Flash from 96 GB of memory. For a 64 GB Mac it points to Qwen3.8 Q2, which this list does not offer yet. You can still choose a model below or add a file you have.',
  downloadAction: size => `Download · ${size}`,
  downloadStatusRunning: 'Downloading',
  downloadProgress: (done, total) => `${done} of ${total}`,
  downloadEta: time => `~${time} left`,
  downloadEtaSeconds: count => `${count} sec`,
  downloadEtaMinutes: count => `${count} min`,
  downloadEtaHours: (hours, minutes) => (minutes ? `${hours} h ${minutes} min` : `${hours} h`),
  downloadPausedLabel: 'Paused',
  downloadChecking: 'Checking the part already downloaded…',
  downloadPauseAction: 'Pause',
  downloadResumeAction: 'Resume',
  downloadDoneToast: model => `${model} is ready.`,
  downloadFailed: model => `Download of ${model} failed`,
  quickstartDetail: (model, size) => `One click sets everything up: the local engine, ${model} (${size} download), and your default for new chats. Nothing leaves this computer.`,
  quickstartDetailReady: model => `One click makes ${model} your default for new chats. Everything runs on this machine.`,
  quickstartAction: 'Set up for me',
  quickstartConfigure: 'Let me choose',
  quickstartPausedDetail: (done, total) => `Paused at ${done} of ${total}. Resume continues where it stopped.`,
  quickstartResume: 'Resume setup',
  quickstartDoneToast: model => `${model} is set up — new chats run on this machine.`,
  quickstartFailed: 'Local model setup failed',
  quickstartStages: ['Engine', 'Model', 'Finish'],
  useAction: 'Use',
  activePill: 'Default',
  activeDetail: 'New chats use this model — it loads when you send your first message',
  activating: 'Starting…',
  activateFailed: model => `Could not switch to ${model}`,
  activateDoneToast: model => `New chats use ${model}.`,
  loadedPill: 'In memory',
  ejectTip: 'Free GPU memory (loads again on the next message)',
  ejected: 'Model unloaded — GPU memory freed.',
  ejectFailed: 'Could not unload the model',
  stopServer: 'Turn off',
  startServer: 'Turn on',
  serverStopped: 'Local server stopped — GPU memory freed.',
  serverStarted: 'Local server running.',
  serverStopFailed: 'Could not stop the local server',
  serverStartFailed: 'Could not start the local server',
  pillFitsGpu: 'Fits your GPU',
  pillTooBig: 'Too big for this machine',
  pillStreams: 'Streams from SSD',
  browseTitle: 'Find more models',
  browseHint: 'ds4 runs only its own models, listed above. Already have one on disk? Add the file: it is linked into your library without copying it.',
  sideloadButton: 'Add model file',
  sideloadTitle: 'Choose a GGUF model file',
  sideloadDone: name => `Added ${name}.`,
  // Two different verbs for two different things: a model Hermes downloaded is removed from this Mac;
  // a file you pointed us at is only forgotten, and says so everywhere.
  removeAction: 'Remove model',
  removeConfirm: model => `Remove ${model}? Its downloaded files are deleted from this Mac.`,
  removed: model => `${model} removed.`,
  removeFailed: 'Remove failed',
  discardAction: 'Discard the partial download',
  discardConfirm: (model, size) => `Discard the ${size} of ${model} downloaded so far? A new download starts from zero.`,
  discarded: model => `The partial download of ${model} was discarded.`,
  discardFailed: 'Discard failed',
  forgetAction: 'Forget model — your file stays where it is',
  forgetConfirm: model => `Forget ${model}? Your file stays where it is.`,
  forgotten: model => `${model} forgotten.`,
  forgetFailed: 'Forget failed',
  linkedPill: 'Added from disk',
  storageTitle: 'Storage',
  storageBarLabel: total => `DwarfStar uses ${total} on this Mac`,
  storageKinds: {
    models: {
      title: 'Downloaded models',
      detail: 'Models Hermes downloaded. Files you added from disk are not counted here and are never touched.',
      action: 'Remove all',
      confirm: size => `Remove all downloaded models (${size})? They are deleted from this Mac, and downloading them again takes a while.`
    },
    cache: {
      title: 'Prompt cache',
      detail: 'Recent conversations ds4 keeps on disk so a model can pick up where it left off. It contains your prompts.',
      action: 'Clear',
      confirm: size => `Clear the prompt cache (${size})? It is rebuilt as you chat.`
    },
    engine: {
      title: 'Engine',
      detail: 'The ds4 server program. Setup installs it again when it is needed.',
      action: 'Remove',
      confirm: size => `Remove the ds4 engine (${size})? Setup installs it again when you need it.`
    }
  },
  storageAllTitle: 'Remove everything',
  storageAllDetail: 'Deletes all of the above, the logs and the settings, so nothing is left behind. Hermes keeps a plugin\'s data when you uninstall it, so do this first if you are removing DwarfStar. Files you added from disk are kept.',
  storageAllAction: 'Remove everything',
  storageAllConfirm: size => `Remove everything DwarfStar stored (${size})? This deletes the engine, the downloaded models and the prompt cache. Files you added from disk are kept. If DwarfStar is your default model, choose another one afterwards.`,
  storageCleared: freed => `Freed ${gb(freed)}.`,
  storageClearFailed: 'Could not free the space',
  originTip: (origin, repo) => `${origin}. Downloaded from Hugging Face: ${repo}.`,
  linkedTip: path => `${path ? `Linked to ${path}. ` : ''}Hermes never copies or deletes this file.`
}

const BACKEND = 'metal'

// A route the gateway does not serve answers "404: <body>", so a failed REQUEST that starts that way
// means the plugin's backend is not loaded. A job's own error text (a string, for example "Hugging Face
// answered 404 for …") is the real cause and is shown as written.
function backendMissing(error) {
  return typeof error !== 'string'
    && (error?.status === 404 || error?.statusCode === 404 || /^404:/.test(error?.message ?? ''))
}

function errorText(error) {
  if (backendMissing(error)) {
    return 'The DwarfStar backend is not loaded on this profile. Enable the plugin with `hermes plugins enable ds4`, then refresh.'
  }
  const message = typeof error === 'string' ? error : error?.message || 'That did not work. Try again.'
  return message.slice(0, 600)
}

// A toast has room for one sentence: for a multi-line error (a load failure ends with the server's
// own last words) keep the first line for context and the last for the cause.
const firstLine = error => {
  const lines = errorText(error).split('\n').map(line => line.trim()).filter(Boolean)
  return (lines.length > 1 ? `${lines[0]} ${lines.at(-1)}` : lines[0] ?? '').slice(0, 300)
}

// Local Models' own size label: binary gigabytes, written GB.
function gb(bytes) {
  return bytes == null ? '—' : `${(bytes / 2 ** 30).toFixed(1)} GB`
}

function formatSpeed(bytesPerSecond) {
  if (!bytesPerSecond || bytesPerSecond <= 0 || !Number.isFinite(bytesPerSecond)) return ''
  const mb = bytesPerSecond / 2 ** 20
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB/s` : mb >= 10 ? `${Math.round(mb)} MB/s` : `${mb.toFixed(1)} MB/s`
}

function formatEta(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 1) return ''
  if (seconds < 60) return copy.downloadEtaSeconds(Math.max(1, Math.round(seconds)))
  const minutes = Math.round(seconds / 60)
  return minutes < 60 ? copy.downloadEtaMinutes(minutes) : copy.downloadEtaHours(Math.floor(minutes / 60), minutes % 60)
}

const isDownloadPhase = job => job.kind === 'download' || (job.kind === 'quickstart' && job.phase === 'downloading')

// One status line for every download surface: what it is doing, how far, how fast, how long left.
function downloadStatusText(job) {
  if (!isDownloadPhase(job)) return job.message
  if (job.checking) return copy.downloadChecking
  const parts = [job.status === 'paused' ? copy.downloadPausedLabel : copy.downloadStatusRunning]
  if (job.total_bytes) parts.push(copy.downloadProgress(gb(job.done_bytes), gb(job.total_bytes)))
  else if (job.done_bytes) parts.push(gb(job.done_bytes))
  if (job.status === 'running') {
    const speed = formatSpeed(job.bytes_per_sec)
    if (speed) {
      parts.push(speed)
      const eta = formatEta(job.eta_seconds)
      if (eta) parts.push(copy.downloadEta(eta))
    }
  }
  return parts.length > 1 ? parts.join(' · ') : job.message || parts[0]
}

function ico(name, className) {
  const Glyph = icons?.[name]
  return Glyph ? jsx(Glyph, className === undefined ? {} : { className }) : null
}

const PILL_VARIANT = { muted: 'muted', primary: 'default', success: 'success', warn: 'warn', destructive: 'destructive' }

// Rest props reach the Badge: a Tip wraps it with hover handlers.
function Pill({ tone = 'muted', children, ...props }) {
  return jsx(Badge, { variant: PILL_VARIANT[tone], ...props, children })
}

function Section({ icon, title, meta, aside, children }) {
  return jsxs('section', { className: 'mb-6', 'aria-label': title, children: [
    jsxs('div', { className: 'mb-2.5 flex items-center gap-2 pt-2 text-[length:var(--conversation-text-font-size)] font-medium', children: [
      ico(icon, 'size-4 shrink-0 text-muted-foreground'),
      jsx('span', { children: title }),
      meta && jsx(Pill, { children: meta }),
      aside && jsx('div', { className: 'ml-auto flex min-w-0 items-center', children: aside })
    ] }),
    children
  ] })
}

function ProgressBar({ percent, paused = false }) {
  const unknown = typeof percent !== 'number'
  return jsx('div', {
    role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, ...(unknown ? {} : { 'aria-valuenow': percent }),
    className: 'h-1.5 w-full overflow-hidden rounded-full bg-(--ui-bg-tertiary)',
    children: jsx('div', {
      className: `h-full rounded-full ${paused ? 'bg-muted-foreground/60' : 'bg-primary transition-[width] duration-300'}`,
      style: { width: `${Math.max(0, Math.min(100, percent ?? 0))}%` }
    })
  })
}

function DownloadProgress({ job }) {
  return jsxs('div', { className: 'grid gap-1', children: [
    jsx(ProgressBar, { paused: job.status === 'paused', percent: job.percent }),
    jsx('p', { className: 'text-[0.68rem] text-muted-foreground', children: downloadStatusText(job) })
  ] })
}

// Pause while bytes move; Paused + Resume for a download that stopped part way.
function DownloadActions({ job, onPause, onResume }) {
  if (job.status === 'paused') {
    return jsxs('div', { className: 'flex items-center justify-end gap-2', children: [
      jsxs(Pill, { tone: 'warn', children: [ico('Pause', 'mr-1 size-3'), copy.downloadPausedLabel] }),
      onResume && jsxs(Button, { size: 'sm', variant: 'outline', onClick: onResume, children: [ico('Play'), copy.downloadResumeAction] })
    ] })
  }
  if (job.status !== 'running' || !job.can_pause) return null
  return jsxs(Button, { size: 'sm', variant: 'outline', onClick: onPause, children: [ico('Pause'), copy.downloadPauseAction] })
}

function Failure({ job }) {
  return job?.status === 'error'
    ? jsx('p', { role: 'alert', className: 'text-[0.75rem] text-destructive break-words whitespace-pre-wrap', children: errorText(job.error) })
    : null
}

// A model Hermes downloaded is removed (trash can); a linked file is only forgotten (cross): the
// icon and the word never suggest that your file is deleted.
function RemoveButton({ model, onRemove }) {
  const [busy, setBusy] = useState(false)
  const linked = model.state === 'linked'
  const partial = model.state === 'available'   // only an unfinished download is on disk
  const label = partial ? copy.discardAction : linked ? copy.forgetAction : copy.removeAction
  const go = async () => {
    setBusy(true)
    try { await onRemove(model) } finally { setBusy(false) }
  }
  return jsx(Tip, { label, children: jsx(Button, {
    size: 'icon', variant: 'ghost', className: busy ? '[&_svg]:animate-spin' : undefined, disabled: busy,
    onClick: go, 'aria-label': `${partial ? 'Discard download of' : linked ? 'Forget' : 'Remove'} ${model.name}`,
    children: ico(busy ? 'Loader2' : linked ? 'X' : 'Trash2')
  }) })
}

// The label for a file that lives where you put it, with its path on hover.
function LinkedPill({ model }) {
  return model.state === 'linked'
    ? jsx(Tip, { label: copy.linkedTip(model.path), children: jsxs(Pill, { children: [ico('Link', 'mr-1 size-3'), copy.linkedPill] }) })
    : null
}

function fitRank(model) {
  return !model.fits ? 2 : model.streaming ? 1 : 0
}

function ModelRow({ model, job, runtimeInstalled, loaded, ramBytes, busy, anyActivation, anyDownload, run, eject, onRemove }) {
  const sideloaded = !model.catalog
  const onDisk = model.state !== 'available'
  // Like Local Models, loading a model keeps its Use button in place (spinning, "Starting…").
  const activating = job?.kind === 'use'
  const downloading = job && isDownloadPhase(job) ? job : null
  const partial = !job && !onDisk && model.partial_bytes > 0
    ? { status: 'paused', kind: 'download', done_bytes: model.partial_bytes, total_bytes: model.size_bytes,
      percent: 100 * model.partial_bytes / model.size_bytes, message: '' }
    : null

  const residency = loaded ? jsx(Pill, { children: copy.loadedPill }) : null
  const use = model.active
    ? (sideloaded
      ? jsxs(Pill, { tone: 'primary', children: [ico('CheckCircle2', 'mr-1 size-3'), copy.activePill] })
      : jsx(Tip, { label: copy.activeDetail, children: jsxs(Pill, { tone: 'primary', children: [ico('Check', 'mr-1 size-3'), copy.activePill] }) }))
    : jsxs(Button, {
      size: 'sm', className: activating ? '[&_svg]:animate-spin' : undefined,
      disabled: busy || anyActivation || !runtimeInstalled, onClick: () => run('/use', { model_id: model.id }),
      'aria-label': `Use ${model.name}`,
      children: [ico(activating ? 'Loader2' : 'Check'), activating ? copy.activating : copy.useAction]
    })
  const onDiskActions = jsxs('div', { className: 'flex items-center justify-end gap-2', children: [
    residency, use,
    loaded && jsx(Tip, { label: copy.ejectTip, children: jsx(Button, { size: 'icon', variant: 'ghost', onClick: eject, 'aria-label': `Unload ${model.name}`, children: ico('Eject') }) }),
    jsx(RemoveButton, { model, onRemove })
  ] })

  const action = onDisk ? onDiskActions
    : downloading ? jsx(DownloadActions, { job: downloading, onPause: () => run('/jobs/cancel', { job_id: downloading.id }) })
      : partial ? jsxs('div', { className: 'flex items-center justify-end gap-2', children: [
        jsx(DownloadActions, { job: partial, onResume: () => run('/models/download', { model_id: model.id }) }),
        jsx(RemoveButton, { model, onRemove })
      ] })
        : jsxs(Button, {
          size: 'sm', variant: 'outline', disabled: busy || !model.fits || anyDownload,
          onClick: () => run('/models/download', { model_id: model.id }), 'aria-label': `Download ${model.name}`,
          children: [ico('Download'), copy.downloadAction(gb(model.size_bytes))]
        })

  const progress = downloading ?? partial
  const below = progress ? jsx('div', { className: 'mt-2', children: jsx(DownloadProgress, { job: progress }) }) : undefined

  if (sideloaded) {
    return jsx(ListRow, {
      action, below,
      description: jsx('span', { className: 'flex flex-wrap items-center gap-1.5', children: jsx(LinkedPill, { model }) }),
      title: jsxs('span', { className: 'inline-flex items-center gap-2', children: [
        jsx('span', { className: 'truncate font-mono text-[0.8rem]', children: model.id }),
        jsx('span', { className: 'text-[0.68rem] font-normal text-muted-foreground', children: gb(model.size_bytes) })
      ] })
    })
  }

  const context = model.launch_ctx ? `${Math.round(model.launch_ctx / 1000)}K` : ''
  const fitPill = !model.fit_known ? null : !model.fits
    ? jsx(Tip, { label: `Needs about ${gb(model.resident_bytes)} of memory; this Mac has ${gb(ramBytes)}.`,
      children: jsxs(Pill, { tone: 'destructive', children: [ico('Cpu', 'mr-1 size-3'), copy.pillTooBig] }) })
    : model.streaming
      ? jsx(Tip, { label: `Works, but slower: it reads its experts from your SSD as needed, so it needs a fast local SSD. Uses about ${gb(model.resident_bytes)} of memory at a ${context} context.`,
        children: jsxs(Pill, { tone: 'warn', children: [ico('Cpu', 'mr-1 size-3'), copy.pillStreams] }) })
      : jsx(Tip, { label: `Runs entirely in memory: about ${gb(model.resident_bytes)} at a ${context} context.`,
        children: jsxs(Pill, { tone: 'success', children: [ico('Cpu', 'mr-1 size-3'), copy.pillFitsGpu] }) })

  const showPills = fitPill || model.state === 'linked'

  const nameLabel = model.origin
    ? jsx(Tip, { label: copy.originTip(model.origin, model.repo), children: jsx('span', { children: model.name }) })
    : model.name

  return jsx(ListRow, {
    action, below,
    description: jsxs('span', { children: [
      model.description,
      showPills && jsxs('span', { className: 'mt-1.5 flex flex-wrap items-center gap-1.5', children: [fitPill, jsx(LinkedPill, { model })] })
    ] }),
    title: jsxs('span', { className: 'inline-flex items-center gap-2', children: [
      nameLabel,
      model.recommended && jsx(Tip, { label: copy.recommendedReason, children: jsx(Pill, { tone: 'primary', children: copy.recommended }) })
    ] })
  })
}

// Setup hero: until something is servable the pane leads with one button that does everything.
function Hero({ job, heroModel, failed, onConfigure, run, stageOf }) {
  const stage = job ? stageOf(job) : 0
  return jsx('div', { className: 'flex min-h-[60dvh] items-center justify-center', children:
    jsxs('div', { className: 'w-full max-w-md text-center', children: [
      jsx('div', { className: 'mx-auto mb-5 flex size-14 items-center justify-center rounded-2xl bg-primary/10', children:
        job ? ico('Loader2', 'size-7 animate-spin text-primary') : ico('Cpu', 'size-7 text-primary') }),
      jsx('h2', { className: 'text-lg font-semibold text-foreground', children: heroModel?.name ?? '' }),
      job ? jsxs('div', { children: [
        jsx('p', { className: 'mt-2 min-h-10 text-[0.8rem] leading-5 text-muted-foreground', children: downloadStatusText(job) }),
        jsx('div', { className: 'mt-5', children: jsx(ProgressBar, { percent: job.percent }) }),
        jsx('div', { className: 'mt-5 flex items-center justify-center gap-3', children:
          jsx(DownloadActions, { job, onPause: () => run('/jobs/cancel', { job_id: job.id }) }) }),
        jsx('div', { className: 'mt-5 flex items-center justify-center gap-5', children: copy.quickstartStages.map((label, i) =>
          jsxs('span', {
            className: `inline-flex items-center gap-1.5 text-[0.72rem] ${i < stage ? 'text-(--ui-text-tertiary)' : i === stage ? 'font-medium text-foreground' : 'text-(--ui-text-tertiary) opacity-60'}`,
            children: [
              i < stage ? ico('CheckCircle2', 'size-3.5 text-primary') : i === stage ? ico('Loader2', 'size-3.5 animate-spin')
                : jsx('span', { className: 'size-1.5 rounded-full bg-current' }),
              label
            ]
          }, label)) })
      ] }) : heroModel && heroModel.state === 'available' && heroModel.partial_bytes > 0 ? jsxs('div', { children: [
        // Pause cancels the job but keeps the partial file: say so, and offer to carry on.
        jsx('p', { className: 'mt-2 text-[0.8rem] leading-5 text-muted-foreground', children:
          copy.quickstartPausedDetail(gb(heroModel.partial_bytes), gb(heroModel.size_bytes)) }),
        jsx('div', { className: 'mt-5', children: jsx(ProgressBar, { paused: true, percent: 100 * heroModel.partial_bytes / heroModel.size_bytes }) }),
        jsxs('div', { className: 'mt-6 flex items-center justify-center gap-3', children: [
          jsx(Button, { size: 'sm', variant: 'outline', onClick: onConfigure, children: copy.quickstartConfigure }),
          jsxs(Button, { onClick: () => run('/quickstart', { model_id: heroModel.id }), children: [ico('Play'), copy.quickstartResume] })
        ] })
      ] }) : heroModel ? jsxs('div', { children: [
        jsx('p', { className: 'mt-2 text-[0.8rem] leading-5 text-muted-foreground', children:
          heroModel.state !== 'available' ? copy.quickstartDetailReady(heroModel.name) : copy.quickstartDetail(heroModel.name, gb(heroModel.size_bytes)) }),
        jsxs('div', { className: 'mt-6 flex items-center justify-center gap-3', children: [
          jsx(Button, { size: 'sm', variant: 'outline', onClick: onConfigure, children: copy.quickstartConfigure }),
          jsxs(Button, { onClick: () => run('/quickstart', { model_id: heroModel.id }), children: [ico('Zap'), copy.quickstartAction] })
        ] })
      ] }) : null,
      failed && jsx('p', { className: 'mt-4 text-[0.75rem] text-destructive', children: errorText(failed.error) })
    ] })
  })
}

// What the plugin keeps on disk, like a phone's Storage page: one bar, then a row per kind of data with
// its size and a button. Hermes keeps a plugin's data when the plugin is uninstalled, so this is where
// the user cleans up first.
const STORAGE_KINDS = ['models', 'cache', 'engine']
const STORAGE_COLORS = { models: 'bg-primary', cache: 'bg-amber-500', engine: 'bg-emerald-500' }

const storageTotal = storage => STORAGE_KINDS.reduce((sum, kind) => sum + (storage?.[kind] ?? 0), 0) + (storage?.logs ?? 0)

function StorageSection({ storage, disabled, onClear }) {
  const total = storageTotal(storage)
  const sizeAndButton = (size, label, onClick, empty) => jsxs('div', { className: 'flex items-center justify-end gap-3', children: [
    jsx('span', { className: 'text-[0.75rem] tabular-nums text-muted-foreground', children: gb(size) }),
    jsxs(Button, { size: 'sm', variant: 'outline', disabled: disabled || empty, onClick, children: [ico('Trash2'), label] })
  ] })

  return jsxs(Section, {
    icon: 'Package', title: copy.storageTitle, meta: gb(total),
    children: [
      jsx('div', {
        role: 'img', 'aria-label': copy.storageBarLabel(gb(total)),
        className: 'mb-2 flex h-2 w-full overflow-hidden rounded-full bg-muted',
        children: STORAGE_KINDS.filter(kind => storage[kind] > 0).map(kind =>
          jsx('div', { className: STORAGE_COLORS[kind], style: { width: `${100 * storage[kind] / total}%` } }, kind))
      }),
      ...STORAGE_KINDS.map(kind => jsx(ListRow, {
        title: copy.storageKinds[kind].title, description: copy.storageKinds[kind].detail,
        action: sizeAndButton(storage[kind], copy.storageKinds[kind].action, () => onClear(kind), !storage[kind])
      }, kind)),
      jsx(ListRow, {
        title: copy.storageAllTitle, description: copy.storageAllDetail,
        action: sizeAndButton(total, copy.storageAllAction, () => onClear('all'), !total)
      }, 'all')
    ]
  })
}

function Page({ ctx, connectionId, profile, epoch }) {
  const client = useQueryClient()
  const queryKey = ['ds4', 'status', connectionId, profile, epoch]
  const [configure, setConfigure] = useState(false)
  const watching = useRef(new Set())   // jobs this page started: their outcome is announced once
  // A reply that arrives after the user switched host or profile belongs to the old one.
  const current = () => gatewayActivationEpoch() === epoch
    && host.state.connectionId.get() === connectionId && host.state.profile.get() === profile
  const request = async (route, options) => {
    const result = await ctx.rest(route, options)
    if (!current()) throw new Error('The host or profile changed. Reopen this page.')
    return result
  }
  const status = useQuery({
    queryKey, queryFn: () => request('/status'), retry: false, refetchOnMount: 'always', refetchOnWindowFocus: true,
    refetchInterval: query => query.state.data?.jobs?.some(job => job.status === 'running') ? 1500 : false
  })
  const action = useMutation({
    mutationFn: ({ route, body }) => request(route, { method: 'POST', body: body || {} }),
    onSettled: () => client.invalidateQueries({ queryKey, exact: true })
  })
  // One door for every request: a toast on success or failure, and a started job is watched to its end.
  const act = async (route, body, { ok, fail } = {}) => {
    try {
      const result = await action.mutateAsync({ route, body })
      if (result?.id) watching.current.add(result.id)
      if (ok) host.notify({ kind: 'success', message: typeof ok === 'function' ? ok(result) : ok })
      return result
    } catch (error) {
      host.notify({ kind: 'error', message: fail ? `${fail}: ${firstLine(error)}` : firstLine(error) })
      return undefined
    }
  }
  const run = (route, body) => { void act(route, body) }

  const data = status.data
  const models = data?.models
  const nameOf = id => models?.find(model => model.id === id)?.name ?? id

  // A job that ends while this page is open says so, like Local Models does.
  useEffect(() => {
    for (const job of data?.jobs ?? []) {
      if (job.status === 'running' || !watching.current.delete(job.id)) continue
      const name = nameOf(job.model_id)
      const toasts = {
        install: [copy.installDoneToast, copy.installFailed],
        download: [copy.downloadDoneToast(name), copy.downloadFailed(name)],
        use: [copy.activateDoneToast(name), copy.activateFailed(name)],
        start: [copy.serverStarted, copy.serverStartFailed],
        quickstart: [copy.quickstartDoneToast(name), copy.quickstartFailed]
      }[job.kind]
      if (!toasts) continue
      if (job.status === 'done') {
        host.notify({ kind: 'success', message: toasts[0] })
        if (job.kind === 'quickstart') host.navigate('/')   // land on a new chat, as the setup flow of Local Models does
      } else if (job.status === 'error') {
        host.notify({ kind: 'error', message: `${toasts[1]}: ${firstLine(job.error)}` })
      }
    }
  }, [data])

  // The same native file dialog Hermes' own Local Models page opens; the path it returns is then
  // linked into the library. Older Desktop builds have no such door: the button says so.
  const canPick = typeof ctx.os?.pickOpenPath === 'function'
  const addFile = async () => {
    const picked = await ctx.os.pickOpenPath({ title: copy.sideloadTitle, filters: [{ name: 'GGUF models', extensions: ['gguf'] }] })
    if (picked) await act('/models/link', { path: picked }, { ok: added => copy.sideloadDone(added.model_id) })
  }
  const remove = async model => {
    if (model.state === 'available') {   // an unfinished download, nothing else of this model on disk
      if (!window.confirm(copy.discardConfirm(model.name, gb(model.partial_bytes)))) return
      await act('/models/delete', { model_id: model.id }, { ok: copy.discarded(model.name), fail: copy.discardFailed })
      return
    }
    const linked = model.state === 'linked'
    if (!window.confirm(linked ? copy.forgetConfirm(model.name) : copy.removeConfirm(model.name))) return
    await act('/models/delete', { model_id: model.id }, linked
      ? { ok: copy.forgotten(model.name), fail: copy.forgetFailed }
      : { ok: copy.removed(model.name), fail: copy.removeFailed })
  }

  const clearStorage = async kind => {
    const size = gb(kind === 'all' ? storageTotal(data.storage) : data.storage[kind])
    const question = kind === 'all' ? copy.storageAllConfirm(size) : copy.storageKinds[kind].confirm(size)
    if (!window.confirm(question)) return
    await act('/storage/clear', { what: kind }, { ok: result => copy.storageCleared(result.freed_bytes), fail: copy.storageClearFailed })
  }

  const jobs = data?.jobs ?? []
  const running = jobs.filter(job => job.status === 'running')
  const busy = action.isPending
  const last = match => jobs.filter(match).at(-1)
  const jobFor = id => running.find(job => job.model_id === id)
  const installJob = running.find(job => job.kind === 'install')
  const serverJob = running.find(job => job.kind === 'start')
  const quickstartJob = running.find(job => job.kind === 'quickstart') ?? null
  const anyActivation = running.some(job => job.kind === 'use' || job.kind === 'quickstart')
  const anyDownload = running.some(isDownloadPhase)
  const active = models?.find(model => model.active)
  const heroModel = models?.find(model => model.recommended && model.fits) ?? null
  const needsSetup = data && (!data.engine.installed || !models.some(model => model.state !== 'available'))
  const stageOf = job => (job.phase === 'starting-server' ? 2 : job.phase === 'downloading' ? 1 : 0)
  const hero = data?.supported && (quickstartJob || (needsSetup && !configure && heroModel))
    && !running.some(job => job.kind === 'install' || job.kind === 'download') && last(job => job.kind === 'install')?.status !== 'error'
  const catalog = models?.filter(model => model.catalog) ?? []
  const sorted = [...catalog].sort((a, b) => fitRank(a) - fitRank(b))

  const shown = []
  if (status.isPending) shown.push(jsxs('div', { className: 'flex items-center gap-2 text-sm', children: [jsx(GlyphSpinner, { ariaLabel: 'Loading' }), 'Loading…'] }, 'loading'))
  if (status.error) shown.push(jsx(ErrorState, {
    title: 'DwarfStar is unavailable', description: errorText(status.error),
    children: jsx(Button, { variant: 'outline', onClick: () => status.refetch(), children: 'Refresh' })
  }, 'error'))
  if (data && data.supported === false) shown.push(jsx(EmptyState, {
    title: 'The managed ds4 runtime needs a Mac with Apple Silicon',
    description: 'On this machine, run ds4-server yourself and choose DwarfStar (ds4) in the model picker.'
  }, 'unsupported'))

  if (hero) {
    shown.push(jsx(Hero, {
      job: quickstartJob, heroModel: quickstartJob ? models.find(model => model.id === quickstartJob.model_id) : heroModel,
      failed: quickstartJob ? null : (last(job => job.kind === 'quickstart')?.status === 'error' ? last(job => job.kind === 'quickstart') : null),
      onConfigure: () => setConfigure(true), run, stageOf
    }, 'hero'))
  } else if (data?.supported) {
    shown.push(
      jsxs(Section, {
        icon: 'Zap', title: copy.runtimeTitle, meta: data.engine.installed ? data.engine.commit : undefined,
        aside: data.engine.installed ? jsx(Pill, { tone: 'primary', children: data.server.running ? copy.serverRunning : copy.runtimeReady(BACKEND) }) : undefined,
        children: [
          data.engine.installed ? jsx(ListRow, {
            title: copy.runtimeInstalled,
            description: data.server.running ? copy.runtimeRunningDetail : copy.runtimeInstalledDetail(data.engine.commit, BACKEND),
            action: serverJob ? jsxs(Button, { size: 'sm', variant: 'outline', disabled: true, className: '[&_svg]:animate-spin', children: [ico('Loader2'), copy.activating] })
              : data.server.running || data.enabled
                ? jsxs(Button, { size: 'sm', variant: 'outline', disabled: busy, onClick: () => act('/server/stop', {}, { ok: copy.serverStopped, fail: copy.serverStopFailed }), children: [ico('StopFilled'), copy.stopServer] })
                : jsxs(Button, { size: 'sm', variant: 'outline', disabled: busy || !active, onClick: () => run('/server/start'), children: [ico('Zap'), copy.startServer] })
          }) : installJob ? jsx(ListRow, {
            title: jsxs('span', { className: 'inline-flex items-center gap-2', children: [ico('Loader2', 'size-3.5 animate-spin'), copy.installing] }),
            description: installJob.message || copy.installing,
            below: jsx(DownloadProgress, { job: installJob })
          }) : jsx(ListRow, {
            title: copy.installTitle, description: copy.installDetail(data.engine.prebuilt),
            action: jsxs(Button, { size: 'sm', disabled: busy, onClick: () => run('/engine/install'), children: [ico('Download'), copy.installAction] })
          }),
          data.engine.installed && jsx(ListRow, {
            title: jsxs('span', { className: 'inline-flex items-center gap-2', children: [ico('CheckCircle2', 'size-4 text-emerald-600 dark:text-emerald-400'), copy.upToDateTitle] }),
            description: copy.upToDateDetail(data.engine.commit, BACKEND)
          }),
          jsx(Failure, { job: last(job => job.kind === 'install' || job.kind === 'start') })
        ]
      }, 'runtime'),
      jsx(Section, {
        icon: 'Monitor', title: copy.hardwareTitle,
        children: jsxs('div', { className: 'flex flex-wrap items-center gap-x-5 gap-y-1 py-1 text-[length:var(--conversation-caption-font-size)] text-muted-foreground', children: [
          jsxs('span', { className: 'inline-flex items-center gap-1.5', children: [ico('Package', 'size-3.5'), copy.ram(gb(data.ram_bytes))] }),
          jsx(Pill, { children: copy.unifiedMemory })
        ] })
      }, 'hardware'),
      jsxs(Section, {
        icon: 'Download', title: copy.modelsTitle, meta: `${catalog.length}`,
        children: [
          !sorted.some(model => model.recommended) && jsx(ListRow, {
            title: copy.noRecommendationTitle, description: copy.noRecommendationDetail
          }, 'no-recommendation'),
          jsx('div', { className: 'grid gap-1', children: [...sorted, ...models.filter(model => !model.catalog)].map(model => jsx(ModelRow, {
            model, job: jobFor(model.id), runtimeInstalled: data.engine.installed,
            loaded: data.server.running && data.server.model === model.id, ramBytes: data.ram_bytes,
            busy, anyActivation, anyDownload, run,
            eject: () => act('/server/eject', {}, { ok: copy.ejected, fail: copy.ejectFailed }), onRemove: remove
          }, model.id)) }),
          jsx(Failure, { job: last(job => job.kind === 'download' || job.kind === 'use') })
        ]
      }, 'models'),
      jsx(Section, {
        icon: 'Search', title: copy.browseTitle,
        aside: jsxs(Button, {
          size: 'sm', variant: 'outline', disabled: busy || !canPick, onClick: addFile,
          title: canPick ? copy.browseHint : 'This version of Hermes Desktop cannot open a file picker for plugins: update it',
          children: [ico('FolderOpen', 'mr-1 size-3.5'), copy.sideloadButton]
        }),
        children: jsx('p', { className: 'py-1 text-[length:var(--conversation-caption-font-size)] text-muted-foreground', children: copy.browseHint })
      }, 'browse'),
      storageTotal(data.storage) > 0 && jsx(StorageSection, {
        storage: data.storage, disabled: busy || running.length > 0, onClear: clearStorage
      }, 'storage')
    )
  }

  return jsxs('div', { className: 'mx-auto w-full max-w-3xl p-4 text-(--ui-text-primary)', children: [
    ...shown,
    action.error && jsx('p', { role: 'alert', className: 'text-[0.75rem] text-destructive break-words', children: errorText(action.error) })
  ] })
}

function Settings({ ctx }) {
  const connectionId = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const epoch = useGatewayActivationEpoch()
  return jsx(Page, { ctx, connectionId, profile, epoch }, JSON.stringify([connectionId, profile, epoch]))
}

export default {
  id: 'ds4',
  name: 'Local Models · DwarfStar',
  register(ctx) {
    ctx.registerSettingsPage({ id: 'settings', title: 'Local Models · DwarfStar', icon: 'server', render: () => jsx(Settings, { ctx }) })
  }
}
