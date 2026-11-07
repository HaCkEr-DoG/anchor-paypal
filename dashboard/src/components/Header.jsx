import React from 'react'
import './Header.css'

export default function Header({ stats }) {
  return (
    <header className="header">
      <div className="header-brand">
        <span className="header-logo">⚓</span>
        <div>
          <span className="header-title">Anchor</span>
          <span className="header-sub">PayPal Dispute Intelligence</span>
        </div>
      </div>
      <div className="header-stats">
        {stats && (
          <>
            <Stat label="Total Disputes" value={stats.disputes_total ?? '—'} />
            <Stat label="Won" value={stats.won ?? '—'} color="green" />
            <Stat label="Pending" value={stats.pending ?? '—'} color="orange" />
            <Stat label="Uptime" value="✓ Live" color="green" />
          </>
        )}
        {!stats && <span className="header-connecting">Connecting to Anchor…</span>}
      </div>
      <div className="header-paypal">
        <img
          src="https://www.paypalobjects.com/webstatic/mktg/Logo/pp-logo-100px.png"
          alt="PayPal"
          height="28"
        />
      </div>
    </header>
  )
}

function Stat({ label, value, color }) {
  return (
    <div className="header-stat">
      <span className={`header-stat-value ${color ? `color-${color}` : ''}`}>{value}</span>
      <span className="header-stat-label">{label}</span>
    </div>
  )
}
