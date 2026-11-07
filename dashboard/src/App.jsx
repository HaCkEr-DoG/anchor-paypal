import React, { useState, useEffect, useCallback } from 'react'
import Header from './components/Header.jsx'
import Sidebar from './components/Sidebar.jsx'
import DisputeQueue from './views/DisputeQueue.jsx'
import MerchantHealth from './views/MerchantHealth.jsx'
import ROICalculator from './views/ROICalculator.jsx'
import BuyerRisk from './views/BuyerRisk.jsx'
import ForecastView from './views/ForecastView.jsx'
import DeadlineTimeline from './views/DeadlineTimeline.jsx'
import './App.css'

const VIEWS = [
  { id: 'queue',    label: 'Dispute Queue',      icon: '⚡' },
  { id: 'timeline', label: 'Deadline Timeline',  icon: '📅' },
  { id: 'health',   label: 'Merchant Health',    icon: '🏪' },
  { id: 'roi',      label: 'Refund ROI',         icon: '💰' },
  { id: 'buyer',    label: 'Buyer Risk',         icon: '👤' },
  { id: 'forecast', label: 'Dispute Forecast',   icon: '📈' },
]

export default function App() {
  const [view, setView] = useState('queue')
  const [stats, setStats] = useState(null)

  useEffect(() => {
    fetch('/api/stats')
      .then(r => r.json())
      .then(setStats)
      .catch(() => {})
  }, [])

  return (
    <div className="app-shell">
      <Header stats={stats} />
      <div className="app-body">
        <Sidebar views={VIEWS} active={view} onNavigate={setView} />
        <main className="app-main">
          {view === 'queue'    && <DisputeQueue />}
          {view === 'timeline' && <DeadlineTimeline />}
          {view === 'health'   && <MerchantHealth />}
          {view === 'roi'      && <ROICalculator />}
          {view === 'buyer'    && <BuyerRisk />}
          {view === 'forecast' && <ForecastView />}
        </main>
      </div>
    </div>
  )
}
