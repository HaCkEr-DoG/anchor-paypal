import React from 'react'
import './Sidebar.css'

export default function Sidebar({ views, active, onNavigate }) {
  return (
    <nav className="sidebar">
      {views.map(v => (
        <button
          key={v.id}
          className={`sidebar-item ${active === v.id ? 'active' : ''}`}
          onClick={() => onNavigate(v.id)}
        >
          <span className="sidebar-icon">{v.icon}</span>
          <span className="sidebar-label">{v.label}</span>
        </button>
      ))}
      <div className="sidebar-footer">
        <span className="sidebar-version">Anchor v67</span>
      </div>
    </nav>
  )
}
