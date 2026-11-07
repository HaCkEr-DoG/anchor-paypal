import React from 'react'
import { Card, StatTile, Badge } from '../components/Card.jsx'
import './BuyerRisk.css'

const RESOLVED = [
  {
    id: 'PP-D-800', buyer: 'alice@email.com', merchant: 'TechGadgets Pro',
    outcome: 'WON_BY_MERCHANT', amount: 349.99, ltv_tier: 'HIGH',
    churn_prob: 0.72, review_prob: 0.58, cb_prob: 0.44,
    overall: 0.72, risk_tier: 'HIGH',
    actions: ['PERSONAL_PHONE_CALL', 'OFFER_DISCOUNT_NEXT_ORDER'],
  },
  {
    id: 'PP-D-801', buyer: 'bob@email.com', merchant: 'StreamPro',
    outcome: 'FULL_REFUND', amount: 24.99, ltv_tier: 'LOW',
    churn_prob: 0.08, review_prob: 0.05, cb_prob: 0.03,
    overall: 0.08, risk_tier: 'LOW',
    actions: ['NO_ACTION_NEEDED'],
  },
  {
    id: 'PP-D-802', buyer: 'carol@email.com', merchant: 'FashionFirst',
    outcome: 'PARTIAL_REFUND', amount: 89.00, ltv_tier: 'MEDIUM',
    churn_prob: 0.44, review_prob: 0.38, cb_prob: 0.22,
    overall: 0.44, risk_tier: 'MEDIUM',
    actions: ['SEND_APOLOGY_EMAIL', 'OFFER_DISCOUNT_NEXT_ORDER'],
  },
  {
    id: 'PP-D-803', buyer: 'dave@email.com', merchant: 'ElectroWorld',
    outcome: 'WON_BY_MERCHANT', amount: 1200.00, ltv_tier: 'VIP',
    churn_prob: 0.81, review_prob: 0.67, cb_prob: 0.73,
    overall: 0.81, risk_tier: 'CRITICAL',
    actions: ['PERSONAL_PHONE_CALL', 'MONITOR_FOR_BANK_CHARGEBACK', 'OFFER_DISCOUNT_NEXT_ORDER'],
  },
]

const TIER_COLORS = { LOW: 'green', MEDIUM: 'orange', HIGH: 'red', CRITICAL: 'red' }
const OUTCOME_COLORS = { WON_BY_MERCHANT: 'orange', FULL_REFUND: 'green', PARTIAL_REFUND: 'blue', ESCALATED_TO_PAYPAL: 'red' }

export default function BuyerRisk() {
  const critical = RESOLVED.filter(r => r.risk_tier === 'CRITICAL').length
  const cbRisk = RESOLVED.filter(r => r.cb_prob > 0.3).length
  const avgChurn = (RESOLVED.reduce((s, r) => s + r.churn_prob, 0) / RESOLVED.length * 100).toFixed(0)

  return (
    <div className="buyer-risk">
      <div className="br-stats">
        <StatTile label="Resolved Disputes" value={RESOLVED.length} color="blue" />
        <StatTile label="Critical Risk" value={critical} color="red" sub="Immediate action" />
        <StatTile label="CB Risk >30%" value={cbRisk} color="orange" sub="Bank chargeback watch" />
        <StatTile label="Avg Churn Risk" value={`${avgChurn}%`} color={parseInt(avgChurn) > 40 ? 'red' : 'orange'} />
      </div>

      <Card title="Post-Dispute Buyer Risk Scores" subtitle="Churn, review, and bank chargeback probability for recently resolved disputes">
        <div className="br-list">
          {RESOLVED.map(r => (
            <div key={r.id} className={`br-item br-item--${TIER_COLORS[r.risk_tier]}`}>
              <div className="br-item-top">
                <div>
                  <span className="br-id">{r.id}</span>
                  <span className="br-buyer">{r.buyer}</span>
                </div>
                <div className="br-badges">
                  <Badge text={r.outcome.replace(/_/g, ' ')} color={OUTCOME_COLORS[r.outcome]} />
                  <Badge text={r.ltv_tier} color={r.ltv_tier === 'VIP' ? 'purple' : 'gray'} />
                  <Badge text={r.risk_tier} color={TIER_COLORS[r.risk_tier]} />
                </div>
              </div>
              <div className="br-probs">
                <Prob label="Churn" value={r.churn_prob} />
                <Prob label="Bad Review" value={r.review_prob} />
                <Prob label="Bank CB" value={r.cb_prob} warn={r.cb_prob > 0.3} />
              </div>
              <div className="br-actions">
                <span className="br-actions-label">Actions:</span>
                {r.actions.map(a => (
                  <Badge key={a} text={a.replace(/_/g, ' ')} color={a === 'NO_ACTION_NEEDED' ? 'green' : a === 'MONITOR_FOR_BANK_CHARGEBACK' ? 'red' : 'blue'} />
                ))}
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}

function Prob({ label, value, warn }) {
  const pct = Math.round(value * 100)
  return (
    <div className={`prob ${warn ? 'prob--warn' : ''}`}>
      <span className="prob-label">{label}</span>
      <div className="prob-bar-wrap">
        <div className="prob-bar" style={{ width: `${pct}%`, background: pct > 60 ? '#ef4444' : pct > 30 ? '#f59e0b' : '#10b981' }} />
      </div>
      <span className="prob-pct">{pct}%</span>
    </div>
  )
}
