import React, { useState } from 'react'
import { Card, StatTile, Badge } from '../components/Card.jsx'
import './DisputeQueue.css'

const DEMO_DISPUTES = [
  {
    id: 'PP-D-001',
    amount: 349.99,
    reason: 'INR',
    status: 'WAITING_FOR_SELLER_RESPONSE',
    deadline: '2026-11-10',
    merchant: 'TechGadgets Pro',
    win_prob: 0.84,
    ev: 112.4,
    recommendation: 'FIGHT',
  },
  {
    id: 'PP-D-002',
    amount: 89.00,
    reason: 'SNAD',
    status: 'WAITING_FOR_SELLER_RESPONSE',
    deadline: '2026-11-09',
    merchant: 'FashionFirst',
    win_prob: 0.31,
    ev: -22.1,
    recommendation: 'REFUND',
  },
  {
    id: 'PP-D-003',
    amount: 1200.00,
    reason: 'UNAUTHORIZED',
    status: 'UNDER_REVIEW',
    deadline: '2026-11-14',
    merchant: 'ElectroWorld',
    win_prob: 0.67,
    ev: 234.8,
    recommendation: 'FIGHT',
  },
  {
    id: 'PP-D-004',
    amount: 24.99,
    reason: 'SUBSCRIPTION',
    status: 'WAITING_FOR_SELLER_RESPONSE',
    deadline: '2026-11-08',
    merchant: 'StreamPro',
    win_prob: 0.55,
    ev: -8.2,
    recommendation: 'REFUND',
  },
  {
    id: 'PP-D-005',
    amount: 599.00,
    reason: 'INR',
    status: 'WAITING_FOR_SELLER_RESPONSE',
    deadline: '2026-11-12',
    merchant: 'LuxBags',
    win_prob: 0.91,
    ev: 387.5,
    recommendation: 'FIGHT',
  },
]

const REASON_LABELS = {
  INR: 'Item Not Received',
  SNAD: 'Significantly Not as Described',
  UNAUTHORIZED: 'Unauthorized Transaction',
  SUBSCRIPTION: 'Subscription Issue',
}

function daysUntil(dateStr) {
  const d = new Date(dateStr) - new Date()
  return Math.ceil(d / (1000 * 60 * 60 * 24))
}

function urgencyColor(days) {
  if (days <= 1) return 'red'
  if (days <= 3) return 'orange'
  return 'green'
}

export default function DisputeQueue() {
  const [selected, setSelected] = useState(null)
  const total = DEMO_DISPUTES.length
  const toFight = DEMO_DISPUTES.filter(d => d.recommendation === 'FIGHT').length
  const totalEV = DEMO_DISPUTES.reduce((s, d) => s + d.ev, 0)

  return (
    <div className="dispute-queue">
      <div className="dq-stats">
        <StatTile label="Open Disputes" value={total} color="blue" />
        <StatTile label="Recommend Fight" value={toFight} color="green" sub={`${total - toFight} refund`} />
        <StatTile label="Total Net EV" value={`$${totalEV.toFixed(0)}`} color={totalEV > 0 ? 'green' : 'red'} />
        <StatTile label="Avg Win Prob" value={`${Math.round(DEMO_DISPUTES.reduce((s,d) => s + d.win_prob, 0) / total * 100)}%`} color="blue" />
      </div>

      <Card title="Open Dispute Queue" subtitle="Sorted by deadline urgency — Anchor oracle recommendations included">
        <table className="dq-table">
          <thead>
            <tr>
              <th>Dispute ID</th>
              <th>Merchant</th>
              <th>Amount</th>
              <th>Reason</th>
              <th>Deadline</th>
              <th>Win Prob</th>
              <th>Net EV</th>
              <th>Recommend</th>
            </tr>
          </thead>
          <tbody>
            {DEMO_DISPUTES.sort((a, b) => daysUntil(a.deadline) - daysUntil(b.deadline)).map(d => {
              const days = daysUntil(d.deadline)
              return (
                <tr
                  key={d.id}
                  className={selected === d.id ? 'selected' : ''}
                  onClick={() => setSelected(selected === d.id ? null : d.id)}
                >
                  <td><code>{d.id}</code></td>
                  <td>{d.merchant}</td>
                  <td><strong>${d.amount.toFixed(2)}</strong></td>
                  <td><Badge text={d.reason} color={d.reason === 'UNAUTHORIZED' ? 'red' : 'blue'} /></td>
                  <td>
                    <span className={`deadline deadline--${urgencyColor(days)}`}>
                      {days <= 0 ? 'TODAY' : `${days}d`}
                    </span>
                  </td>
                  <td>
                    <span className={d.win_prob >= 0.6 ? 'prob-high' : d.win_prob >= 0.4 ? 'prob-mid' : 'prob-low'}>
                      {Math.round(d.win_prob * 100)}%
                    </span>
                  </td>
                  <td className={d.ev >= 0 ? 'ev-pos' : 'ev-neg'}>
                    {d.ev >= 0 ? '+' : ''}${d.ev.toFixed(1)}
                  </td>
                  <td>
                    <Badge
                      text={d.recommendation}
                      color={d.recommendation === 'FIGHT' ? 'green' : 'orange'}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </Card>

      {selected && (() => {
        const d = DEMO_DISPUTES.find(x => x.id === selected)
        return (
          <Card title={`Detail: ${d.id}`} subtitle={REASON_LABELS[d.reason]}>
            <div className="dq-detail">
              <div className="dq-detail-row">
                <span>Merchant</span><strong>{d.merchant}</strong>
              </div>
              <div className="dq-detail-row">
                <span>Dispute Amount</span><strong>${d.amount.toFixed(2)}</strong>
              </div>
              <div className="dq-detail-row">
                <span>Win Probability</span>
                <strong>{Math.round(d.win_prob * 100)}%</strong>
              </div>
              <div className="dq-detail-row">
                <span>Expected Value</span>
                <strong className={d.ev >= 0 ? 'ev-pos' : 'ev-neg'}>
                  {d.ev >= 0 ? '+' : ''}${d.ev.toFixed(2)}
                </strong>
              </div>
              <div className="dq-detail-row">
                <span>Anchor Recommendation</span>
                <Badge text={d.recommendation} color={d.recommendation === 'FIGHT' ? 'green' : 'orange'} />
              </div>
              <div className="dq-insight">
                {d.recommendation === 'FIGHT'
                  ? `✅ Fight this dispute. ${Math.round(d.win_prob * 100)}% win probability with net EV of +$${d.ev.toFixed(2)} makes fighting the optimal strategy. Gather evidence and respond before deadline.`
                  : `💸 Refund this dispute. Despite ${Math.round(d.win_prob * 100)}% win probability, response costs make refunding more economical (EV: $${d.ev.toFixed(2)}). Issue refund to preserve buyer relationship.`
                }
              </div>
            </div>
          </Card>
        )
      })()}
    </div>
  )
}
