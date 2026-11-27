import type { Register } from 'claude-code'

const PRIVKEY_AS_MERCHANT_RE = /(?:merchant|payTo|merchant_wallet)\s*[:=]\s*["']?0x[a-fA-F0-9]{64}\b/i
const BURN_MERCHANT_RE = /(?:merchant|payTo|merchant_wallet)\s*[:=]\s*["']?0x0{36}(?:0000|dead)\b/i

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    $.ui.log(`paygate-x402-guard active in ${e.cwd}`)
    return r
  })

  on('tool.call', { tool: 'Bash' }, async ($, e, next) => {
    const cmd = typeof e.command === 'string' ? e.command : ''
    if (PRIVKEY_AS_MERCHANT_RE.test(cmd)) {
      return { deny: 'PayGate Guard blocked a 64-hex private key in merchant/payTo; pass a 40-hex public receiving address.' }
    }
    if (BURN_MERCHANT_RE.test(cmd)) {
      return { deny: 'PayGate Guard blocked EVM zero/burn address in merchant/payTo payout target.' }
    }
    return next(e)
  })
}
