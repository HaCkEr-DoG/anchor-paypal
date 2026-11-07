import React, { useState } from 'react'
import { Card, StatTile, Badge } from '../components/Card.jsx'
import './ROICalculator.css'

const DEFAULT = {
  dispute_id: 'PP-D-CALC',
  merchant_id: 'MY_STORE',
  dispute_amount: '200',
  reason_code: 'INR',
  win_probability: '0.65',
  merchant_segment: 'SMALL_BUSINESS',
  buyer_ltv: '400',
  is_repeat_buyer: true,
  buyer_prior_orders: '3',
}

export default function ROICalculator() {
  const [form, setForm] = useState(DEFAULT)
  const [result, setResult] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  function set(k, v) { setForm(f => ({ ...f, [k]: v })) }

  async function calculate() {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/oracle/refund-roi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...form,
          dispute_amount: parseFloat(form.dispute_amount),
          win_probability: parseFloat(form.win_probability),
          buyer_ltv: parseFloat(form.buyer_ltv),
          buyer_prior_orders: parseInt(form.buyer_prior_orders),
        }),
      })
      if (!res.ok) throw new Error(await res.text())
      setResult(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  // Local calculation as fallback
  function calculateLocal() {
    const amount = parseFloat(form.dispute_amount)
    const p = parseFloat(form.win_probability)
    const ltv = parseFloat(form.buyer_ltv)
    const rcMap = {
      ENTERPRISE:     { INR: 8,  SNAD: 20, UNAUTHORIZED: 35, SUBSCRIPTION: 15 },
      MID_MARKET:     { INR: 15, SNAD: 30, UNAUTHORIZED: 55, SUBSCRIPTION: 25 },
      SMALL_BUSINESS: { INR: 25, SNAD: 50, UNAUTHORIZED: 80, SUBSCRIPTION: 40 },
      MICRO:          { INR: 40, SNAD: 75, UNAUTHORIZED: 120,SUBSCRIPTION: 60 },
    }
    const rc = (rcMap[form.merchant_segment] || rcMap.SMALL_BUSINESS)[form.reason_code] || 30
    const cb = 20
    const ev_fight = p * (amount - rc) - (1 - p) * (amount + cb + rc)
    const retention = ltv * (0.15 + (form.is_repeat_buyer ? 0.05 : 0) + (parseInt(form.buyer_prior_orders) >= 5 ? 0.05 : 0))
    const ev_refund = -amount + retention
    const recommended = ev_fight > ev_refund ? 'FIGHT' : 'REFUND'
    setResult({
      dispute_id: form.dispute_id,
      merchant_id: form.merchant_id,
      recommended_action: recommended,
      confidence: Math.abs(ev_fight - ev_refund) > 20 ? 'HIGH' : 'MEDIUM',
      ev_fight: parseFloat(ev_fight.toFixed(2)),
      ev_refund: parseFloat(ev_refund.toFixed(2)),
      ev_partial_refund: parseFloat(((ev_fight + ev_refund) / 2).toFixed(2)),
      fight_refund_delta: parseFloat((ev_fight - ev_refund).toFixed(2)),
      is_fight_roi_positive: ev_fight > 0,
      is_refund_roi_better: ev_refund > ev_fight,
      response_cost_used: rc,
      chargeback_fee_used: cb,
      key_insight: `${recommended === 'FIGHT' ? '✅ Fight' : '💸 Refund'} this dispute. EV(fight)=$${ev_fight.toFixed(2)}, EV(refund)=$${ev_refund.toFixed(2)}.`,
      paypal_signal: `Merchant ${form.merchant_id}: ${recommended} recommended for $${amount} ${form.reason_code} dispute.`,
    })
    setError(null)
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    calculateLocal()
    calculate().catch(() => {})
  }

  return (
    <div className="roi-calc">
      <div className="roi-layout">
        <Card title="Dispute Parameters" subtitle="Enter dispute details to compute ROI">
          <form onSubmit={handleSubmit} className="roi-form">
            <FormRow label="Dispute Amount ($)">
              <input type="number" value={form.dispute_amount} onChange={e => set('dispute_amount', e.target.value)} min="0" step="0.01" />
            </FormRow>
            <FormRow label="Reason Code">
              <select value={form.reason_code} onChange={e => set('reason_code', e.target.value)}>
                <option value="INR">INR — Item Not Received</option>
                <option value="SNAD">SNAD — Significantly Not as Described</option>
                <option value="UNAUTHORIZED">UNAUTHORIZED — Unauthorized Transaction</option>
                <option value="SUBSCRIPTION">SUBSCRIPTION — Subscription Issue</option>
              </select>
            </FormRow>
            <FormRow label="Win Probability (0-1)">
              <input type="number" value={form.win_probability} onChange={e => set('win_probability', e.target.value)} min="0" max="1" step="0.01" />
            </FormRow>
            <FormRow label="Merchant Segment">
              <select value={form.merchant_segment} onChange={e => set('merchant_segment', e.target.value)}>
                <option value="ENTERPRISE">Enterprise</option>
                <option value="MID_MARKET">Mid Market</option>
                <option value="SMALL_BUSINESS">Small Business</option>
                <option value="MICRO">Micro</option>
              </select>
            </FormRow>
            <FormRow label="Buyer Lifetime Value ($)">
              <input type="number" value={form.buyer_ltv} onChange={e => set('buyer_ltv', e.target.value)} min="0" />
            </FormRow>
            <FormRow label="Repeat Buyer?">
              <label className="toggle">
                <input type="checkbox" checked={form.is_repeat_buyer} onChange={e => set('is_repeat_buyer', e.target.checked)} />
                <span>{form.is_repeat_buyer ? 'Yes' : 'No'}</span>
              </label>
            </FormRow>
            <FormRow label="Prior Orders at Merchant">
              <input type="number" value={form.buyer_prior_orders} onChange={e => set('buyer_prior_orders', e.target.value)} min="0" />
            </FormRow>
            <button type="submit" className="roi-submit" disabled={loading}>
              {loading ? 'Calculating…' : 'Calculate ROI'}
            </button>
          </form>
        </Card>

        <div className="roi-results">
          {result ? (
            <>
              <div className="roi-ev-grid">
                <EVCard label="EV: Fight" value={result.ev_fight} />
                <EVCard label="EV: Refund" value={result.ev_refund} />
                <EVCard label="EV: Partial" value={result.ev_partial_refund} />
              </div>
              <Card title="Recommendation" subtitle={`Confidence: ${result.confidence}`}>
                <div className="roi-rec">
                  <div className={`roi-rec-action ${result.recommended_action === 'FIGHT' ? 'roi-fight' : 'roi-refund'}`}>
                    {result.recommended_action === 'FIGHT' ? '⚔️ FIGHT' : '💸 REFUND'}
                  </div>
                  <div className="roi-insight">{result.key_insight}</div>
                  <div className="roi-row">
                    <span>Response Cost (ops)</span><strong>${result.response_cost_used}</strong>
                  </div>
                  <div className="roi-row">
                    <span>Chargeback Fee</span><strong>${result.chargeback_fee_used}</strong>
                  </div>
                  <div className="roi-row">
                    <span>Fight vs. Refund Delta</span>
                    <strong className={result.fight_refund_delta >= 0 ? 'ev-pos' : 'ev-neg'}>
                      ${result.fight_refund_delta >= 0 ? '+' : ''}{result.fight_refund_delta}
                    </strong>
                  </div>
                  <div className="roi-paypal">
                    <strong>PayPal Signal:</strong> {result.paypal_signal}
                  </div>
                </div>
              </Card>
            </>
          ) : (
            <Card title="Results will appear here" subtitle="Fill in the form and click Calculate ROI">
              <div className="roi-empty">
                <span>💰</span>
                <p>Enter dispute details to see the expected value breakdown for each decision path.</p>
              </div>
            </Card>
          )}
          {error && <div className="roi-error">API error: {error} (showing local calculation above)</div>}
        </div>
      </div>
    </div>
  )
}

function FormRow({ label, children }) {
  return (
    <div className="form-row">
      <label>{label}</label>
      {children}
    </div>
  )
}

function EVCard({ label, value }) {
  const pos = value >= 0
  return (
    <div className={`ev-card ${pos ? 'ev-card--pos' : 'ev-card--neg'}`}>
      <div className="ev-card-label">{label}</div>
      <div className="ev-card-value">{pos ? '+' : ''}${value?.toFixed(2)}</div>
    </div>
  )
}
