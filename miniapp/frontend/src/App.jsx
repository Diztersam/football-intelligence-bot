import { useCallback, useEffect, useState } from 'react'
import {
  Activity, CalendarDays, RefreshCw, ShieldCheck,
  Target, AlertCircle, CheckCircle2, LoaderCircle, Trophy
} from 'lucide-react'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')
const telegram = window.Telegram?.WebApp
telegram?.ready?.()
telegram?.expand?.()

function formatKickoff(fixture) {
  const value = fixture.kickoff_lagos || fixture.kickoff_utc
  if (!value) return 'Kickoff time unavailable'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('en-NG', {
    timeZone: 'Africa/Lagos',
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hour12: true
  }).format(date)
}

function percent(value) {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '—'
}

function authHeaders() {
  const initData = telegram?.initData || ''
  return {
    'Content-Type': 'application/json',
    ...(initData ? { 'X-Telegram-Init-Data': initData } : {})
  }
}

function App() {
  const [fixtures, setFixtures] = useState([])
  const [predictions, setPredictions] = useState([])
  const [loading, setLoading] = useState(true)
  const [predicting, setPredicting] = useState(false)
  const [jobStatus, setJobStatus] = useState('idle')
  const [jobMessage, setJobMessage] = useState('Press Predict Matches to analyse up to five upcoming football fixtures.')
  const [error, setError] = useState('')

  const request = useCallback(async (path, options = {}) => {
    const response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers: { ...authHeaders(), ...(options.headers || {}) }
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      throw new Error(data.detail || `API request failed (${response.status})`)
    }
    return data
  }, [])

  const loadFixtures = useCallback(async () => {
    const data = await request('/api/fixtures?limit=50')
    setFixtures(Array.isArray(data.fixtures) ? data.fixtures : [])
  }, [request])

  const loadPredictions = useCallback(async () => {
    const data = await request('/api/predictions?limit=5')
    setPredictions(Array.isArray(data.predictions) ? data.predictions : [])
  }, [request])

  const refreshAll = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      await Promise.all([loadFixtures(), loadPredictions()])
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [loadFixtures, loadPredictions])

  useEffect(() => {
    refreshAll()
  }, [refreshAll])

  const pollJob = useCallback(async () => {
    try {
      const data = await request('/api/predict/status')
      setJobStatus(data.status || 'idle')
      setJobMessage(data.message || 'Checking prediction status…')
      if (data.status === 'completed') {
        setPredictions(Array.isArray(data.predictions) ? data.predictions : [])
        await loadFixtures()
        setPredicting(false)
      } else if (data.status === 'failed') {
        setPredicting(false)
        if (data.error) setError(data.error)
      }
    } catch (err) {
      setPredicting(false)
      setError(err.message)
    }
  }, [request, loadFixtures])

  useEffect(() => {
    if (!['queued', 'running'].includes(jobStatus)) return undefined
    const timer = window.setTimeout(pollJob, 2500)
    return () => window.clearTimeout(timer)
  }, [jobStatus, pollJob])

  async function predictMatches() {
    if (predicting) return
    setPredicting(true)
    setError('')
    setJobStatus('queued')
    setJobMessage('Sending your request to the football analysis engine…')
    try {
      const data = await request('/api/predict', { method: 'POST' })
      setJobStatus(data.status || 'queued')
      setJobMessage(data.message || 'Prediction job queued.')
    } catch (err) {
      setPredicting(false)
      setJobStatus('failed')
      setJobMessage('Could not start the prediction run.')
      setError(err.message)
    }
  }

  const isBusy = predicting || ['queued', 'running'].includes(jobStatus)

  return (
    <div className="min-h-screen bg-[#0b1512] text-[#e5eee9]">
      <header className="sticky top-0 z-20 flex h-16 items-center justify-between border-b border-white/10 bg-[#0b1512]/95 px-5 backdrop-blur md:px-8">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-xl bg-emerald-500/15 text-emerald-400">
            <Activity size={21} />
          </div>
          <div>
            <h1 className="text-sm font-bold tracking-wide">
              FOOTBALL <span className="text-emerald-400">INTELLIGENCE</span>
            </h1>
            <p className="text-[9px] tracking-[2px] text-gray-500">MATCH PREDICTION APP</p>
          </div>
        </div>
        <span className="flex items-center gap-2 text-[10px] text-emerald-400">
          <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-400" />
          FOOTBALL ONLY
        </span>
      </header>

      <main className="mx-auto max-w-5xl p-4 md:p-8">
        <section className="mb-7 rounded-2xl border border-emerald-500/15 bg-gradient-to-br from-[#14271e] to-[#101d17] p-5 md:p-7">
          <p className="mb-2 text-[10px] tracking-[2px] text-emerald-400">YOUR MATCH INTELLIGENCE</p>
          <h2 className="text-2xl font-bold md:text-3xl">Football predictions</h2>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-400">
            Analyse upcoming football fixtures using your existing data and prediction engine.
            Analysis starts only when you press the button.
          </p>
          <button
            onClick={predictMatches}
            disabled={isBusy}
            className="mt-6 flex w-full items-center justify-center gap-3 rounded-xl bg-emerald-500 px-5 py-4 text-sm font-extrabold tracking-wide text-[#07110d] transition hover:bg-emerald-400 disabled:cursor-not-allowed disabled:opacity-60 sm:w-auto sm:min-w-64"
          >
            {isBusy ? <LoaderCircle size={19} className="animate-spin" /> : <Target size={19} />}
            {isBusy ? 'ANALYSIS RUNNING…' : 'PREDICT MATCHES'}
          </button>

          <div className="mt-4 flex items-start gap-2 text-xs text-gray-400">
            {jobStatus === 'completed'
              ? <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-emerald-400" />
              : isBusy
              ? <LoaderCircle size={15} className="mt-0.5 shrink-0 animate-spin text-amber-300" />
              : <ShieldCheck size={15} className="mt-0.5 shrink-0 text-emerald-400" />}
            <span>{jobMessage}</span>
          </div>
        </section>

        {error && (
          <div className="mb-6 flex items-start gap-3 rounded-xl border border-red-500/20 bg-red-500/10 p-4 text-sm text-red-300">
            <AlertCircle size={18} className="mt-0.5 shrink-0" />
            <div className="min-w-0">
              <p className="font-semibold">Connection or analysis issue</p>
              <p className="mt-1 break-words text-xs">{error}</p>
              <button onClick={refreshAll} className="mt-3 text-xs underline">Retry connection</button>
            </div>
          </div>
        )}

        <section className="mb-7">
          <div className="mb-4 flex items-center justify-between gap-3">
            <div>
              <p className="text-[10px] tracking-[2px] text-emerald-400">FIXTURE LIST</p>
              <h3 className="mt-1 text-lg font-bold">Upcoming matches</h3>
            </div>
            <button
              onClick={refreshAll}
              disabled={loading}
              aria-label="Refresh fixtures"
              className="rounded-lg border border-white/10 p-2 text-gray-400 hover:border-emerald-500/40 hover:text-emerald-400 disabled:opacity-50"
            >
              <RefreshCw size={16} className={loading ? 'animate-spin' : ''} />
            </button>
          </div>

          {loading ? (
            <div className="rounded-xl border border-white/5 bg-[#111f19] py-10 text-center text-sm text-gray-500">
              <LoaderCircle size={22} className="mx-auto mb-3 animate-spin text-emerald-400" />
              Loading upcoming fixtures…
            </div>
          ) : fixtures.length === 0 ? (
            <div className="rounded-xl border border-white/5 bg-[#111f19] py-10 text-center text-sm text-gray-500">
              <Trophy size={24} className="mx-auto mb-3 opacity-50" />
              No upcoming football fixtures were found in the database.
            </div>
          ) : (
            <div className="space-y-3">
              {fixtures.map(fixture => (
                <article key={fixture.event_id} className="rounded-xl border border-white/5 bg-[#111f19] p-4">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[10px] tracking-wide text-emerald-400">{fixture.league || 'Football'}</span>
                    <span className="flex items-center gap-1 text-[10px] text-gray-500">
                      <CalendarDays size={12} /> {formatKickoff(fixture)}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="flex-1 text-sm font-semibold">{fixture.home_team}</span>
                    <span className="text-[10px] font-bold text-gray-600">VS</span>
                    <span className="flex-1 text-right text-sm font-semibold">{fixture.away_team}</span>
                  </div>
                </article>
              ))}
            </div>
          )}
        </section>

        <section>
          <div className="mb-4">
            <p className="text-[10px] tracking-[2px] text-emerald-400">ENGINE OUTPUT</p>
            <h3 className="mt-1 text-lg font-bold">Latest predictions</h3>
          </div>

          {predictions.length === 0 ? (
            <div className="rounded-xl border border-white/5 bg-[#111f19] p-5 text-sm text-gray-500">
              No saved predictions to display yet. Press <span className="font-semibold text-emerald-400">Predict Matches</span> to start a new run.
            </div>
          ) : (
            <div className="space-y-3">
              {predictions.map(item => (
                <article key={item.prediction_id} className="rounded-xl border border-emerald-500/10 bg-[#111f19] p-4 md:p-5">
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[10px] tracking-wide text-emerald-400">{item.league}</span>
                    <span className="text-[10px] text-gray-500">{formatKickoff(item)}</span>
                  </div>
                  <h4 className="text-sm font-bold md:text-base">{item.home_team} <span className="mx-1 text-gray-600">vs</span> {item.away_team}</h4>
                  <div className="mt-4 grid grid-cols-3 gap-2">
                    <div className="rounded-lg bg-[#0b1512] p-3 text-center">
                      <p className="text-[9px] text-gray-500">HOME</p>
                      <p className="mt-1 text-sm font-bold">{percent(item.home_win)}</p>
                    </div>
                    <div className="rounded-lg bg-[#0b1512] p-3 text-center">
                      <p className="text-[9px] text-gray-500">DRAW</p>
                      <p className="mt-1 text-sm font-bold">{percent(item.draw_probability)}</p>
                    </div>
                    <div className="rounded-lg bg-[#0b1512] p-3 text-center">
                      <p className="text-[9px] text-gray-500">AWAY</p>
                      <p className="mt-1 text-sm font-bold">{percent(item.away_win)}</p>
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="text-gray-400">Confidence: <strong className="text-emerald-300">{percent(item.confidence)}</strong></span>
                    <span className="rounded-md bg-emerald-400/10 px-2 py-1 text-emerald-300">
                      {item.best_candidate?.market || 'No qualifying selection'}
                    </span>
                  </div>
                  <p className="mt-2 text-[10px] text-gray-600">
                    Data quality: {item.data_quality || 'Not specified'} · Estimates, not guarantees.
                  </p>
                </article>
              ))}
            </div>
          )}
        </section>

        <p className="mt-8 text-center text-[10px] text-gray-600">
          FOOTBALL INTELLIGENCE · MAXIMUM FIVE MATCHES PER RUN · NO AUTOMATIC BETTING
        </p>
      </main>
    </div>
  )
}

export default App
