import React from 'react'
import './Card.css'

export function Card({ title, subtitle, children, className = '' }) {
  return (
    <div className={`card ${className}`}>
      {(title || subtitle) && (
        <div className="card-header">
          {title && <h3 className="card-title">{title}</h3>}
          {subtitle && <p className="card-subtitle">{subtitle}</p>}
        </div>
      )}
      <div className="card-body">{children}</div>
    </div>
  )
}

export function StatTile({ label, value, sub, color }) {
  return (
    <div className={`stat-tile ${color ? `stat-tile--${color}` : ''}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  )
}

export function Badge({ text, color = 'blue' }) {
  return <span className={`badge badge--${color}`}>{text}</span>
}

export function ProgressBar({ value, max, color = 'blue' }) {
  const pct = Math.round((value / max) * 100)
  return (
    <div className="progress-wrap">
      <div
        className={`progress-bar progress-bar--${color}`}
        style={{ width: `${Math.min(100, pct)}%` }}
      />
      <span className="progress-label">{pct}%</span>
    </div>
  )
}
