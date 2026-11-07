/**
 * DeadlineTimeline — Bryntum-inspired dispute deadline Gantt view.
 *
 * Uses the Bryntum Scheduler visual language (resource rows + time bars) to
 * show the merchant's open disputes as a timeline grid. Each dispute is a
 * "resource row"; its remaining response window is the bar. Color = urgency tier.
 *
 * In a production deployment this would mount @bryntum/scheduler.
 * For the hackathon sandbox we render a pixel-perfect recreation using CSS
 * Grid + SVG so the demo works without an npm install of the licensed package,
 * but the data model and rendering logic mirror the Bryntum Scheduler API.
 */

import React, { useState } from 'react'
import './DeadlineTimeline.css'

const TODAY = new Date('2026-11-07')
const WINDOW_DAYS = 14

// Sample disputes with deadline urgency
const DISPUTES = [
  { id: 'PP-D-10041', amount: 299.00, reason: 'INR',          deadline: 0, priority: 'P0', ev: 180, win: 0.72 },
  { id: 'PP-D-10042', amount: 850.00, reason: 'UNAUTHORIZED', deadline: 1, priority: 'P0', ev: 540, win: 0.80 },
  { id: 'PP-D-10043', amount: 149.00, reason: 'SNAD',         deadline: 2, priority: 'P1', ev: 60,  win: 0.55 },
  { id: 'PP-D-10044', amount: 420.00, reason: 'INR',          deadline: 3, priority: 'P1', ev: 290, win: 0.68 },
  { id: 'PP-D-10045', amount: 59.99,  reason: 'SUBSCRIPTION', deadline: 5, priority: 'P2', ev: 20,  win: 0.45 },
  { id: 'PP-D-10046', amount: 1200.0, reason: 'UNAUTHORIZED', deadline: 7, priority: 'P2', ev: 780, win: 0.76 },
  { id: 'PP-D-10047', amount: 89.00,  reason: 'INR',          deadline: 10, priority: 'P3', ev: 40, win: 0.60 },
  { id: 'PP-D-10048', amount: 35.00,  reason: 'SUBSCRIPTION', deadline: 14, priority: 'P4', ev: 5,  win: 0.30 },
]

const PRIORITY_COLORS = {
  P0: '#ef4444',
  P1: '#f59e0b',
  P2: '#3b82f6',
  P3: '#10b981',
  P4: '#94a3b8',
}

const PRIORITY_LABELS = {
  P0: 'CRITICAL',
  P1: 'HIGH',
  P2: 'MEDIUM',
  P3: 'LOW',
  P4: 'DEFER',
}

function dayLabel(offset) {
  const d = new Date(TODAY)
  d.setDate(d.getDate() + offset)
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

export default function DeadlineTimeline() {
  const [selected, setSelected] = useState(null)
  const p0count = DISPUTES.filter(d => d.priority === 'P0').length
  const totalEV = DISPUTES.reduce((s, d) => s + d.ev, 0)

  return (
    <div className="dt-root">
      {/* Stats row */}
      <div className="dt-stats">
        <div className="dt-stat">
          <div className="dt-stat-val" style={{ color: '#ef4444' }}>{p0count}</div>
          <div className="dt-stat-label">P0 Critical</div>
        </div>
        <div className="dt-stat">
          <div className="dt-stat-val">{DISPUTES.length}</div>
          <div className="dt-stat-label">Open Disputes</div>
        </div>
        <div className="dt-stat">
          <div className="dt-stat-val" style={{ color: '#10b981' }}>${totalEV.toLocaleString()}</div>
          <div className="dt-stat-label">Total EV at Stake</div>
        </div>
        <div className="dt-stat">
          <div className="dt-stat-val" style={{ color: '#f59e0b' }}>
            {DISPUTES.filter(d => d.deadline <= 3).length}
          </div>
          <div className="dt-stat-label">Due This Week</div>
        </div>
      </div>

      {/* Bryntum-style header */}
      <div className="dt-header-row">
        <div className="dt-row-label-col dt-header-cell">
          Dispute
        </div>
        <div className="dt-timeline-col">
          <div className="dt-day-headers">
            {Array.from({ length: WINDOW_DAYS + 1 }, (_, i) => (
              <div key={i} className="dt-day-header" style={{ left: `${(i / WINDOW_DAYS) * 100}%` }}>
                {i % 2 === 0 ? dayLabel(i) : ''}
              </div>
            ))}
          </div>
          {/* Today line */}
          <div className="dt-today-line" />
          {/* Weekend shading */}
          {Array.from({ length: WINDOW_DAYS }, (_, i) => {
            const d = new Date(TODAY)
            d.setDate(d.getDate() + i)
            const isWeekend = d.getDay() === 0 || d.getDay() === 6
            return isWeekend ? (
              <div
                key={i}
                className="dt-weekend"
                style={{ left: `${(i / WINDOW_DAYS) * 100}%`, width: `${(1 / WINDOW_DAYS) * 100}%` }}
              />
            ) : null
          })}
        </div>
      </div>

      {/* Dispute rows */}
      <div className="dt-rows">
        {[...DISPUTES].sort((a, b) => a.deadline - b.deadline).map(d => {
          const color = PRIORITY_COLORS[d.priority]
          const pct = (d.deadline / WINDOW_DAYS) * 100
          const isSelected = selected === d.id

          return (
            <div
              key={d.id}
              className={`dt-row ${isSelected ? 'dt-row--selected' : ''}`}
              onClick={() => setSelected(isSelected ? null : d.id)}
            >
              {/* Row label */}
              <div className="dt-row-label-col">
                <div className="dt-dispute-id" style={{ color }}>
                  {d.id}
                </div>
                <div className="dt-dispute-meta">
                  ${d.amount.toFixed(2)} · {d.reason}
                </div>
              </div>

              {/* Timeline cell */}
              <div className="dt-timeline-col dt-timeline-cell">
                {/* Gridlines */}
                {Array.from({ length: WINDOW_DAYS }, (_, i) => (
                  <div key={i} className="dt-gridline"
                    style={{ left: `${(i / WINDOW_DAYS) * 100}%` }} />
                ))}

                {/* Bar: from today (0) to deadline */}
                <div
                  className="dt-bar"
                  style={{
                    left: '0%',
                    width: `${Math.max(0.5, pct)}%`,
                    background: color,
                    opacity: 0.85,
                  }}
                >
                  <span className="dt-bar-label">
                    {d.deadline === 0 ? 'TODAY' : `${d.deadline}d`}
                  </span>
                </div>

                {/* Deadline pin */}
                <div
                  className="dt-deadline-pin"
                  style={{ left: `${pct}%`, background: color }}
                  title={`Deadline: ${dayLabel(d.deadline)}`}
                />
              </div>
            </div>
          )
        })}
      </div>

      {/* Detail panel */}
      {selected && (() => {
        const d = DISPUTES.find(x => x.id === selected)
        const color = PRIORITY_COLORS[d.priority]
        return (
          <div className="dt-detail" style={{ borderColor: color }}>
            <div className="dt-detail-header" style={{ background: color }}>
              <span>{d.id}</span>
              <span className="dt-priority-badge">{PRIORITY_LABELS[d.priority]}</span>
            </div>
            <div className="dt-detail-body">
              <div className="dt-detail-row">
                <span>Amount</span><span>${d.amount.toFixed(2)}</span>
              </div>
              <div className="dt-detail-row">
                <span>Reason</span><span>{d.reason}</span>
              </div>
              <div className="dt-detail-row">
                <span>Deadline</span>
                <span style={{ color }}>
                  {d.deadline === 0 ? '⚠ TODAY' : `${d.deadline} day${d.deadline !== 1 ? 's' : ''}`}
                </span>
              </div>
              <div className="dt-detail-row">
                <span>Expected Value</span><span style={{ color: '#10b981' }}>${d.ev}</span>
              </div>
              <div className="dt-detail-row">
                <span>Win Probability</span><span>{(d.win * 100).toFixed(0)}%</span>
              </div>
              <div className="dt-action" style={{ background: color }}>
                {d.priority === 'P0' ? 'RESPOND NOW — Deadline imminent' :
                 d.priority === 'P1' ? 'Gather evidence today' :
                 d.priority === 'P2' ? 'Schedule this week' :
                 d.priority === 'P4' ? 'Consider refunding' :
                 'Monitor and schedule'}
              </div>
            </div>
          </div>
        )
      })()}

      {/* Attribution */}
      <div className="dt-attribution">
        Powered by <strong>Anchor</strong> × <strong>Bryntum Scheduler</strong> visual model
      </div>
    </div>
  )
}
