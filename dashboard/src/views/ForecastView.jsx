import React from 'react'
import { Card, StatTile, Badge } from '../components/Card.jsx'
import './ForecastView.css'

const ORDERS = [
  {
    id: 'ORD-5001', merchant: 'TechGadgets Pro', amount: 299.00,
    tier: 'LOW', prob: 0.04, likely_type: 'NONE',
    signals: ['digital_goods_signal'],
    prevention: ['Enable instant delivery confirmation'],
  },
  {
    id: 'ORD-5002', merchant: 'FashionFirst', amount: 149.00,
    tier: 'HIGH', prob: 0.24, likely_type: 'INR',
    signals: ['no_tracking_signal', 'new_buyer_signal'],
    prevention: ['Add tracking number immediately', 'Send proactive shipping update'],
  },
  {
    id: 'ORD-5003', merchant: 'ElectroWorld', amount: 899.00,
    tier: 'MEDIUM', prob: 0.18, likely_type: 'SNAD',
    signals: ['high_value_no_sig_signal', 'peak_season_signal'],
    prevention: ['Require signature confirmation', 'Add delivery insurance'],
  },
  {
    id: 'ORD-5004', merchant: 'StreamPro', amount: 49.99,
    tier: 'CRITICAL', prob: 0.38, likely_type: 'SUBSCRIPTION',
    signals: ['subscription_signal', 'prior_dispute_signal'],
    prevention: ['Send renewal reminder 7 days prior', 'Add cancellation self-service link'],
  },
  {
    id: 'ORD-5005', merchant: 'LuxBags', amount: 2200.00,
    tier: 'HIGH', prob: 0.21, likely_type: 'INR',
    signals: ['international_signal', 'high_value_no_sig_signal'],
    prevention: ['Add signature confirmation', 'Enable international tracking'],
  },
]

const TIER_COLORS = { LOW: 'green', MEDIUM: 'orange', HIGH: 'red', CRITICAL: 'red' }

export default function ForecastView() {
  const high = ORDERS.filter(o => o.tier === 'HIGH' || o.tier === 'CRITICAL').length
  const avgProb = (ORDERS.reduce((s, o) => s + o.prob, 0) / ORDERS.length * 100).toFixed(0)
  const preventable = ORDERS.filter(o => o.prevention.length > 0).length

  return (
    <div className="forecast-view">
      <div className="fv-stats">
        <StatTile label="Orders Scored" value={ORDERS.length} color="blue" />
        <StatTile label="High Risk" value={high} color="red" sub="Immediate action" />
        <StatTile label="Preventable" value={preventable} color="green" sub="With intervention" />
        <StatTile label="Avg Dispute Prob" value={`${avgProb}%`} color={parseInt(avgProb) > 15 ? 'red' : 'orange'} />
      </div>

      <Card title="Post-Fulfillment Dispute Forecast" subtitle="Anchor scores orders AFTER shipment for dispute risk — before buyers file">
        <div className="fv-list">
          {ORDERS.sort((a, b) => b.prob - a.prob).map(o => (
            <div key={o.id} className={`fv-item fv-item--${TIER_COLORS[o.tier]}`}>
              <div className="fv-item-header">
                <div className="fv-order-info">
                  <code>{o.id}</code>
                  <span className="fv-merchant">{o.merchant}</span>
                  <strong>${o.amount.toFixed(2)}</strong>
                </div>
                <div className="fv-badges">
                  <Badge text={o.tier} color={TIER_COLORS[o.tier]} />
                  {o.likely_type !== 'NONE' && (
                    <Badge text={`→ ${o.likely_type}`} color="blue" />
                  )}
                </div>
              </div>
              <div className="fv-prob-row">
                <span className="fv-prob-label">Dispute Probability</span>
                <div className="fv-prob-bar-wrap">
                  <div
                    className="fv-prob-bar"
                    style={{
                      width: `${Math.round(o.prob * 100)}%`,
                      background: o.prob > 0.3 ? '#ef4444' : o.prob > 0.15 ? '#f59e0b' : '#10b981',
                    }}
                  />
                </div>
                <span className="fv-prob-pct">{Math.round(o.prob * 100)}%</span>
              </div>
              <div className="fv-signals">
                <span className="fv-label">Risk signals:</span>
                {o.signals.map(s => (
                  <Badge key={s} text={s.replace(/_signal$/, '').replace(/_/g, ' ')} color="orange" />
                ))}
              </div>
              {o.prevention.length > 0 && (
                <div className="fv-prevention">
                  <span className="fv-label">Prevention actions:</span>
                  <ul>
                    {o.prevention.map((p, i) => <li key={i}>{p}</li>)}
                  </ul>
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
