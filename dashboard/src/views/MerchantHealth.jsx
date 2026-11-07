import React from 'react'
import { Card, StatTile, Badge, ProgressBar } from '../components/Card.jsx'
import './MerchantHealth.css'

const MERCHANTS = [
  {
    id: 'MERCH_001', name: 'TechGadgets Pro',
    health_score: 87, tier: 'GOOD',
    dispute_rate: 0.012, win_rate: 0.78,
    avg_response_days: 2.1, total_orders: 4200,
    top_issues: ['FULFILLMENT_DELAYS', 'EVIDENCE_WEAKNESS'],
    paypal_opportunity: 'High growth merchant — recommend premium seller tools',
    projected_improvement: 92,
  },
  {
    id: 'MERCH_002', name: 'FashionFirst',
    health_score: 54, tier: 'FAIR',
    dispute_rate: 0.034, win_rate: 0.41,
    avg_response_days: 6.8, total_orders: 1800,
    top_issues: ['PRODUCT_QUALITY', 'COMMUNICATION_GAP'],
    paypal_opportunity: 'At risk — route to merchant success coaching',
    projected_improvement: 72,
  },
  {
    id: 'MERCH_003', name: 'ElectroWorld',
    health_score: 71, tier: 'GOOD',
    dispute_rate: 0.021, win_rate: 0.69,
    avg_response_days: 3.4, total_orders: 8900,
    top_issues: ['FRAUD_EXPOSURE'],
    paypal_opportunity: 'Enable advanced fraud prevention tools',
    projected_improvement: 83,
  },
  {
    id: 'MERCH_004', name: 'StreamPro',
    health_score: 22, tier: 'CRITICAL',
    dispute_rate: 0.082, win_rate: 0.28,
    avg_response_days: 12.4, total_orders: 5600,
    top_issues: ['SUBSCRIPTION_CONFUSION', 'RESPONSE_LATENCY'],
    paypal_opportunity: 'CRITICAL — monitor for account suspension risk',
    projected_improvement: 55,
  },
  {
    id: 'MERCH_005', name: 'LuxBags',
    health_score: 94, tier: 'EXCELLENT',
    dispute_rate: 0.004, win_rate: 0.91,
    avg_response_days: 1.2, total_orders: 2300,
    top_issues: [],
    paypal_opportunity: 'Elite performer — showcase in seller success stories',
    projected_improvement: 95,
  },
]

const TIER_COLORS = {
  EXCELLENT: 'green',
  GOOD: 'blue',
  FAIR: 'orange',
  POOR: 'red',
  CRITICAL: 'red',
}

export default function MerchantHealth() {
  const avg = Math.round(MERCHANTS.reduce((s, m) => s + m.health_score, 0) / MERCHANTS.length)
  const critical = MERCHANTS.filter(m => m.tier === 'CRITICAL' || m.tier === 'POOR').length
  const excellent = MERCHANTS.filter(m => m.tier === 'EXCELLENT' || m.tier === 'GOOD').length

  return (
    <div className="merchant-health">
      <div className="mh-stats">
        <StatTile label="Avg Health Score" value={avg} color="blue" sub="/ 100" />
        <StatTile label="At Risk" value={critical} color="red" sub="POOR or CRITICAL" />
        <StatTile label="Healthy" value={excellent} color="green" sub="GOOD or EXCELLENT" />
        <StatTile label="Merchants Monitored" value={MERCHANTS.length} color="blue" />
      </div>

      <Card title="Merchant Health Portfolio" subtitle="Anchor health scores across all monitored merchants — dual benefit: merchant success + PayPal risk management">
        <div className="mh-grid">
          {MERCHANTS.map(m => (
            <MerchantCard key={m.id} merchant={m} />
          ))}
        </div>
      </Card>
    </div>
  )
}

function MerchantCard({ merchant: m }) {
  const color = TIER_COLORS[m.tier] || 'blue'
  return (
    <div className={`mh-card mh-card--${color}`}>
      <div className="mh-card-header">
        <span className="mh-name">{m.name}</span>
        <Badge text={m.tier} color={color} />
      </div>
      <div className="mh-score-row">
        <span className="mh-score">{m.health_score}</span>
        <span className="mh-score-label">/ 100</span>
        <span className="mh-projected">→ {m.projected_improvement} projected</span>
      </div>
      <div className="mh-metrics">
        <Metric label="Dispute Rate" value={`${(m.dispute_rate * 100).toFixed(1)}%`} bad={m.dispute_rate > 0.03} />
        <Metric label="Win Rate" value={`${Math.round(m.win_rate * 100)}%`} bad={m.win_rate < 0.5} />
        <Metric label="Avg Response" value={`${m.avg_response_days}d`} bad={m.avg_response_days > 5} />
      </div>
      {m.top_issues.length > 0 && (
        <div className="mh-issues">
          {m.top_issues.map(i => (
            <Badge key={i} text={i.replace(/_/g, ' ')} color="orange" />
          ))}
        </div>
      )}
      <div className="mh-paypal">{m.paypal_opportunity}</div>
    </div>
  )
}

function Metric({ label, value, bad }) {
  return (
    <div className="mh-metric">
      <span className="mh-metric-label">{label}</span>
      <span className={`mh-metric-value ${bad ? 'bad' : 'good'}`}>{value}</span>
    </div>
  )
}
