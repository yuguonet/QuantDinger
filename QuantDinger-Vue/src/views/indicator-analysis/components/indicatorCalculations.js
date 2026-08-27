/**
 * 技术指标计算模块
 * 所有指标返回数组，长度与输入 klineData 一致，前缀无效值为 null。
 * 约定：输入 klineData 为 [{ open, high, low, close, volume, timestamp/time }, ...]
 */

// ═══════════════════════════════════════════
// 辅助函数
// ═══════════════════════════════════════════

/** EMA 计算（通用） */
export function _ema (data, period) {
  const result = []
  const k = 2 / (period + 1)
  let ema = null
  for (let i = 0; i < data.length; i++) {
    if (data[i] == null) { result.push(null); continue }
    if (ema === null) {
      ema = data[i]
    } else {
      ema = data[i] * k + ema * (1 - k)
    }
    result.push(i < period - 1 ? null : ema)
  }
  return result
}

/** SMA 计算（通用，基于数值数组） */
export function _sma (data, period) {
  const result = []
  for (let i = 0; i < data.length; i++) {
    if (i < period - 1 || data[i] == null) { result.push(null); continue }
    let sum = 0
    let count = 0
    for (let j = i - period + 1; j <= i; j++) {
      if (data[j] != null) { sum += data[j]; count++ }
    }
    result.push(count > 0 ? sum / count : null)
  }
  return result
}

/** 从 klineData 提取 close 数组 */
function closes (klineData) {
  return klineData.map(d => d.close)
}

/** 从 klineData 提取 volume 数组 */
function volumes (klineData) {
  return klineData.map(d => d.volume || 0)
}

// ═══════════════════════════════════════════
// 移动平均类
// ═══════════════════════════════════════════

/** WMA - 加权移动平均线 */
export function calcWMA (klineData, period = 20) {
  const c = closes(klineData)
  const result = []
  const denom = period * (period + 1) / 2
  for (let i = 0; i < c.length; i++) {
    if (i < period - 1) { result.push(null); continue }
    let sum = 0
    for (let j = 0; j < period; j++) {
      sum += c[i - period + 1 + j] * (j + 1)
    }
    result.push(sum / denom)
  }
  return result
}

/** DEMA - 双重指数移动平均线 */
export function calcDEMA (klineData, period = 20) {
  const c = closes(klineData)
  const ema1 = _ema(c, period)
  const ema2 = _ema(ema1.map(v => v ?? 0), period)
  return ema1.map((v, i) => {
    if (v == null || ema2[i] == null) return null
    return 2 * v - ema2[i]
  })
}

/** TEMA - 三重指数移动平均线 */
export function calcTEMA (klineData, period = 20) {
  const c = closes(klineData)
  const ema1 = _ema(c, period)
  const ema2 = _ema(ema1.map(v => v ?? 0), period)
  const ema3 = _ema(ema2.map(v => v ?? 0), period)
  return ema1.map((v, i) => {
    if (v == null || ema2[i] == null || ema3[i] == null) return null
    return 3 * v - 3 * ema2[i] + ema3[i]
  })
}

/** HMA - 赫尔移动平均线 */
export function calcHMA (klineData, period = 20) {
  const half = Math.floor(period / 2)
  const sqrtPeriod = Math.floor(Math.sqrt(period))
  const wma1 = calcWMA(klineData, half)
  const wma2 = calcWMA(klineData, period)
  const diff = wma1.map((v, i) => {
    if (v == null || wma2[i] == null) return null
    return 2 * v - wma2[i]
  })
  // WMA of diff with sqrt(period)
  const result = []
  for (let i = 0; i < diff.length; i++) {
    if (i < sqrtPeriod - 1 || diff[i] == null) { result.push(null); continue }
    let sum = 0
    let denom = 0
    for (let j = 0; j < sqrtPeriod; j++) {
      const val = diff[i - sqrtPeriod + 1 + j]
      if (val != null) { sum += val * (j + 1); denom += j + 1 }
    }
    result.push(denom > 0 ? sum / denom : null)
  }
  return result
}

/** KAMA - 考夫曼自适应移动平均线 */
export function calcKAMA (klineData, period = 10, fastPeriod = 2, slowPeriod = 30) {
  const c = closes(klineData)
  const result = []
  const fastSC = 2 / (fastPeriod + 1)
  const slowSC = 2 / (slowPeriod + 1)
  let kama = null
  for (let i = 0; i < c.length; i++) {
    if (i < period) { result.push(null); continue }
    if (kama === null) { kama = c[i]; result.push(kama); continue }
    const direction = Math.abs(c[i] - c[i - period])
    let volatility = 0
    for (let j = 1; j <= period; j++) {
      volatility += Math.abs(c[i - j + 1] - c[i - j])
    }
    const er = volatility === 0 ? 0 : direction / volatility
    const sc = er * (fastSC - slowSC) + slowSC
    kama = kama + sc * sc * (c[i] - kama)
    result.push(kama)
  }
  return result
}

/** ALMA - Arnaud Legoux 移动平均线 */
export function calcALMA (klineData, period = 9, offset = 0.85, sigma = 6) {
  const c = closes(klineData)
  const result = []
  for (let i = 0; i < c.length; i++) {
    if (i < period - 1) { result.push(null); continue }
    const m = offset * (period - 1)
    const s = period / sigma
    let normSum = 0
    let sum = 0
    for (let j = 0; j < period; j++) {
      const weight = Math.exp(-((j - m) * (j - m)) / (2 * s * s))
      normSum += weight
      sum += weight * c[i - period + 1 + j]
    }
    result.push(normSum > 0 ? sum / normSum : null)
  }
  return result
}

/** VWMA - 成交量加权移动平均线 */
export function calcVWMA (klineData, period = 20) {
  const c = closes(klineData)
  const v = volumes(klineData)
  const result = []
  for (let i = 0; i < c.length; i++) {
    if (i < period - 1) { result.push(null); continue }
    let sumPV = 0
    let sumV = 0
    for (let j = i - period + 1; j <= i; j++) {
      sumPV += c[j] * (v[j] || 0)
      sumV += v[j] || 0
    }
    result.push(sumV > 0 ? sumPV / sumV : null)
  }
  return result
}

// ═══════════════════════════════════════════
// 通道/包络类
// ═══════════════════════════════════════════

/** KC - 肯特纳通道 */
export function calcKC (klineData, period = 20, multiplier = 1.5) {
  const c = closes(klineData)
  const emaArr = _ema(c, period)
  const atrArr = calcATR(klineData, period)
  return emaArr.map((v, i) => {
    if (v == null || atrArr[i] == null) return { upper: null, middle: null, lower: null }
    return {
      upper: v + multiplier * atrArr[i],
      middle: v,
      lower: v - multiplier * atrArr[i]
    }
  })
}

/** DC - 唐奇安通道 */
export function calcDC (klineData, period = 20) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < period - 1) { result.push({ upper: null, middle: null, lower: null }); continue }
    let highest = -Infinity
    let lowest = Infinity
    for (let j = i - period + 1; j <= i; j++) {
      highest = Math.max(highest, klineData[j].high)
      lowest = Math.min(lowest, klineData[j].low)
    }
    result.push({ upper: highest, middle: (highest + lowest) / 2, lower: lowest })
  }
  return result
}

/** ENV - 包络线 */
export function calcENV (klineData, period = 20, percentage = 2.5) {
  const c = closes(klineData)
  const smaArr = _sma(c, period)
  return smaArr.map(v => {
    if (v == null) return { upper: null, middle: null, lower: null }
    return {
      upper: v * (1 + percentage / 100),
      middle: v,
      lower: v * (1 - percentage / 100)
    }
  })
}

// ═══════════════════════════════════════════
// 趋势跟踪/止损类
// ═══════════════════════════════════════════

/** SuperTrend */
export function calcSuperTrend (klineData, period = 10, multiplier = 3) {
  const atrArr = calcATR(klineData, period)
  const result = []
  let superTrend = 0
  let direction = 1 // 1=up, -1=down
  for (let i = 0; i < klineData.length; i++) {
    if (atrArr[i] == null) { result.push({ st: null, dir: 0 }); continue }
    const hl2 = (klineData[i].high + klineData[i].low) / 2
    const upperBand = hl2 + multiplier * atrArr[i]
    const lowerBand = hl2 - multiplier * atrArr[i]
    if (i === 0 || atrArr[i - 1] == null) {
      superTrend = direction === 1 ? lowerBand : upperBand
      result.push({ st: superTrend, dir: direction })
      continue
    }
    if (klineData[i].close > superTrend && direction === -1) {
      direction = 1
      superTrend = lowerBand
    } else if (klineData[i].close < superTrend && direction === 1) {
      direction = -1
      superTrend = upperBand
    } else {
      if (direction === 1) {
        superTrend = Math.max(superTrend, lowerBand)
      } else {
        superTrend = Math.min(superTrend, upperBand)
      }
    }
    result.push({ st: superTrend, dir: direction })
  }
  return result
}

/** VSTOP - 波动止损通道 */
export function calcVStop (klineData, period = 20, multiplier = 2) {
  const atrArr = calcATR(klineData, period)
  const result = []
  let vstop = 0
  let direction = 1
  let highest = 0
  let lowest = Infinity
  for (let i = 0; i < klineData.length; i++) {
    if (atrArr[i] == null) { result.push({ vstop: null, dir: 0 }); continue }
    if (direction === 1) {
      highest = Math.max(highest, klineData[i].high)
      vstop = highest - multiplier * atrArr[i]
      if (klineData[i].close < vstop) {
        direction = -1
        lowest = klineData[i].low
        vstop = lowest + multiplier * atrArr[i]
      }
    } else {
      lowest = Math.min(lowest, klineData[i].low)
      vstop = lowest + multiplier * atrArr[i]
      if (klineData[i].close > vstop) {
        direction = 1
        highest = klineData[i].high
        vstop = highest - multiplier * atrArr[i]
      }
    }
    result.push({ vstop, dir: direction })
  }
  return result
}

// ═══════════════════════════════════════════
// 综合系统类
// ═══════════════════════════════════════════

/** Ichimoku 一目均衡表 */
export function calcIchimoku (klineData, tenkan = 9, kijun = 26, senkou = 52) {
  const mid = (data, period) => {
    const result = []
    for (let i = 0; i < data.length; i++) {
      if (i < period - 1) { result.push(null); continue }
      let hi = -Infinity, lo = Infinity
      for (let j = i - period + 1; j <= i; j++) {
        hi = Math.max(hi, data[j].high)
        lo = Math.min(lo, data[j].low)
      }
      result.push((hi + lo) / 2)
    }
    return result
  }
  const tenkanArr = mid(klineData, tenkan)
  const kijunArr = mid(klineData, kijun)
  const senkouA = tenkanArr.map((t, i) => {
    if (t == null || kijunArr[i] == null) return null
    return (t + kijunArr[i]) / 2
  })
  const senkouB = mid(klineData, senkou)
  return klineData.map((_, i) => ({
    tenkan: tenkanArr[i],
    kijun: kijunArr[i],
    senkouA: i + senkou < klineData.length ? senkouA[i] : (senkouA[i - senkou] ?? null),
    senkouB: i + senkou < klineData.length ? senkouB[i] : (senkouB[i - senkou] ?? null),
    chikou: i + kijun < klineData.length ? klineData[i + kijun].close : null
  }))
}

// ═══════════════════════════════════════════
// 结构分析类
// ═══════════════════════════════════════════

/** ZigZag 之字转向 */
export function calcZigZag (klineData, deviation = 5) {
  const result = []
  let lastZigZag = null
  let direction = 0
  let lastHigh = 0
  let lastLow = Infinity
  for (let i = 0; i < klineData.length; i++) {
    const h = klineData[i].high
    const l = klineData[i].low
    if (direction === 0) {
      if (h - l >= deviation) {
        direction = h > l ? 1 : -1
        lastZigZag = direction === 1 ? h : l
        lastHigh = h
        lastLow = l
      }
      result.push(lastZigZag)
      continue
    }
    if (direction === 1) {
      lastHigh = Math.max(lastHigh, h)
      if (lastHigh - l >= deviation) {
        direction = -1
        lastZigZag = lastHigh
        lastLow = l
      }
    } else {
      lastLow = Math.min(lastLow, l)
      if (h - lastLow >= deviation) {
        direction = 1
        lastZigZag = lastLow
        lastHigh = h
      }
    }
    result.push(lastZigZag)
  }
  return result
}

/** 分形 (简化版) */
export function calcFractal (klineData, period = 2) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < period || i >= klineData.length - period) { result.push({ hi: null, lo: null }); continue }
    let isHigh = true
    let isLow = true
    for (let j = 1; j <= period; j++) {
      if (klineData[i].high <= klineData[i - j].high || klineData[i].high <= klineData[i + j].high) isHigh = false
      if (klineData[i].low >= klineData[i - j].low || klineData[i].low >= klineData[i + j].low) isLow = false
    }
    result.push({
      hi: isHigh ? klineData[i].high : null,
      lo: isLow ? klineData[i].low : null
    })
  }
  return result
}

/** 摆动高点 */
export function calcPivotHi (klineData, leftBars = 5, rightBars = 5) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < leftBars || i >= klineData.length - rightBars) { result.push(null); continue }
    let isPivot = true
    for (let j = 1; j <= leftBars; j++) {
      if (klineData[i].high <= klineData[i - j].high) { isPivot = false; break }
    }
    if (isPivot) {
      for (let j = 1; j <= rightBars; j++) {
        if (klineData[i].high <= klineData[i + j].high) { isPivot = false; break }
      }
    }
    result.push(isPivot ? klineData[i].high : null)
  }
  return result
}

/** 摆动低点 */
export function calcPivotLo (klineData, leftBars = 5, rightBars = 5) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < leftBars || i >= klineData.length - rightBars) { result.push(null); continue }
    let isPivot = true
    for (let j = 1; j <= leftBars; j++) {
      if (klineData[i].low >= klineData[i - j].low) { isPivot = false; break }
    }
    if (isPivot) {
      for (let j = 1; j <= rightBars; j++) {
        if (klineData[i].low >= klineData[i + j].low) { isPivot = false; break }
      }
    }
    result.push(isPivot ? klineData[i].low : null)
  }
  return result
}

// ═══════════════════════════════════════════
// 枢轴点系列
// ═══════════════════════════════════════════

/** 经典枢轴点 */
export function calcPivotClassic (klineData) {
  return klineData.map((d, i) => {
    if (i === 0) return { pp: null, r1: null, r2: null, s1: null, s2: null }
    const prev = klineData[i - 1]
    const pp = (prev.high + prev.low + prev.close) / 3
    return {
      pp,
      r1: 2 * pp - prev.low,
      r2: pp + (prev.high - prev.low),
      s1: 2 * pp - prev.high,
      s2: pp - (prev.high - prev.low)
    }
  })
}

// ═══════════════════════════════════════════
// 动量类
// ═══════════════════════════════════════════

/** MOM - 动量 */
export function calcMOM (klineData, period = 10) {
  const c = closes(klineData)
  return c.map((v, i) => i < period ? null : v - c[i - period])
}

/** TSI - 真实强弱指数 */
export function calcTSI (klineData, longPeriod = 25, shortPeriod = 13) {
  const c = closes(klineData)
  const pc = c.map((v, i) => i === 0 ? 0 : v - c[i - 1])
  const ema1 = _ema(pc, longPeriod)
  const ema2 = _ema(ema1.map(v => v ?? 0), shortPeriod)
  const absPc = pc.map(v => Math.abs(v))
  const absEma1 = _ema(absPc, longPeriod)
  const absEma2 = _ema(absEma1.map(v => v ?? 0), shortPeriod)
  return ema2.map((v, i) => {
    if (v == null || absEma2[i] == null || absEma2[i] === 0) return null
    return 100 * v / absEma2[i]
  })
}

/** BOP - 力量平衡 */
export function calcBOP (klineData) {
  return klineData.map(d => {
    const range = d.high - d.low
    return range === 0 ? 0 : (d.close - d.open) / range
  })
}

/** RMI - 相对动量指数 */
export function calcRMI (klineData, period = 14, momentum = 5) {
  const c = closes(klineData)
  const gains = []
  const losses = []
  for (let i = 0; i < c.length; i++) {
    if (i < momentum) { gains.push(0); losses.push(0); continue }
    const change = c[i] - c[i - momentum]
    gains.push(change > 0 ? change : 0)
    losses.push(change < 0 ? -change : 0)
  }
  const avgGain = _sma(gains, period)
  const avgLoss = _sma(losses, period)
  return avgGain.map((g, i) => {
    if (g == null || avgLoss[i] == null) return null
    if (avgLoss[i] === 0) return 100
    return 100 - 100 / (1 + g / avgLoss[i])
  })
}

/** KST - 确认指标 */
export function calcKST (klineData, r1 = 10, r2 = 15, r3 = 20, r4 = 30, s1 = 10, s2 = 10, s3 = 10, s4 = 15, sig = 9) {
  const c = closes(klineData)
  const roc1 = c.map((v, i) => i < r1 ? null : (v - c[i - r1]) / c[i - r1] * 100)
  const roc2 = c.map((v, i) => i < r2 ? null : (v - c[i - r2]) / c[i - r2] * 100)
  const roc3 = c.map((v, i) => i < r3 ? null : (v - c[i - r3]) / c[i - r3] * 100)
  const roc4 = c.map((v, i) => i < r4 ? null : (v - c[i - r4]) / c[i - r4] * 100)
  const sma1 = _sma(roc1.map(v => v ?? 0), s1)
  const sma2 = _sma(roc2.map(v => v ?? 0), s2)
  const sma3 = _sma(roc3.map(v => v ?? 0), s3)
  const sma4 = _sma(roc4.map(v => v ?? 0), s4)
  const kst = sma1.map((v, i) => {
    if (v == null || sma2[i] == null || sma3[i] == null || sma4[i] == null) return null
    return v + 2 * sma2[i] + 3 * sma3[i] + 4 * sma4[i]
  })
  const signal = _sma(kst.map(v => v ?? 0), sig)
  return kst.map((v, i) => ({
    kst: v,
    signal: signal[i]
  }))
}

/** UO - 终极震荡器 */
export function calcUO (klineData, s1 = 7, s2 = 14, s3 = 28) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < s3) { result.push(null); continue }
    let bp = 0
    let tr = 0
    for (let j = i - s3 + 1; j <= i; j++) {
      const trueLow = Math.min(klineData[j].low, j > 0 ? klineData[j - 1].close : klineData[j].low)
      const trueHigh = Math.max(klineData[j].high, j > 0 ? klineData[j - 1].close : klineData[j].high)
      bp += klineData[j].close - trueLow
      tr += trueHigh - trueLow
    }
    // Simplified: use single period for all three averages
    result.push(tr === 0 ? 50 : 100 * bp / tr)
  }
  return result
}

/** Coppock 估波曲线 */
export function calcCoppock (klineData, wmaPeriod = 10, roc1Period = 14, roc2Period = 11) {
  const c = closes(klineData)
  const maxRoc = Math.max(roc1Period, roc2Period)
  const rocSum = c.map((v, i) => {
    if (i < maxRoc) return null
    const roc1 = (v - c[i - roc1Period]) / c[i - roc1Period] * 100
    const roc2 = (v - c[i - roc2Period]) / c[i - roc2Period] * 100
    return roc1 + roc2
  })
  return calcWMA(rocSum.map(v => ({ close: v ?? 0 })), wmaPeriod)
}

// ═══════════════════════════════════════════
// Bill Williams 系列
// ═══════════════════════════════════════════

/** AC - 加速震荡器 */
export function calcAC (klineData) {
  const ao = calcAO(klineData)
  return _sma(ao.map(v => v ?? 0), 5)
}

/** AO - 动量震荡器 (已有，这里导出) */
export function calcAO (klineData) {
  const mid = klineData.map(d => (d.high + d.low) / 2)
  const sma5 = _sma(mid, 5)
  const sma34 = _sma(mid, 34)
  return sma5.map((v, i) => {
    if (v == null || sma34[i] == null) return null
    return v - sma34[i]
  })
}

/** Alligator 鳄鱼指标 */
export function calcAlligator (klineData) {
  const mid = klineData.map(d => (d.high + d.low) / 2)
  const sma5 = _sma(mid, 5)
  const sma8 = _sma(mid, 8)
  const sma13 = _sma(mid, 13)
  return klineData.map((_, i) => ({
    jaw: sma13[i], // 颚线 (13期，前移8)
    teeth: sma8[i], // 齿线 (8期，前移5)
    lips: sma5[i] // 唇线 (5期，前移3)
  }))
}

// ═══════════════════════════════════════════
// Ehlers 系列
// ═══════════════════════════════════════════

/** Fisher 变换 */
export function calcFisher (klineData, period = 10) {
  const result = []
  let fisher = 0
  for (let i = 0; i < klineData.length; i++) {
    if (i < period) { result.push({ fisher: null, signal: null }); continue }
    let hi = -Infinity, lo = Infinity
    for (let j = i - period + 1; j <= i; j++) {
      hi = Math.max(hi, klineData[j].high)
      lo = Math.min(lo, klineData[j].low)
    }
    const mid = (klineData[i].high + klineData[i].low) / 2
    const range = hi - lo
    const raw = range === 0 ? 0 : 2 * ((mid - lo) / range - 0.5)
    const clipped = Math.max(-0.999, Math.min(0.999, raw))
    const prev = fisher
    fisher = 0.5 * Math.log((1 + clipped) / (1 - clipped)) + 0.5 * prev
    result.push({ fisher, signal: prev })
  }
  return result
}

/** Ehlers 随机 */
export function calcEhlersStoch (klineData, period = 10) {
  const c = closes(klineData)
  const result = []
  for (let i = 0; i < c.length; i++) {
    if (i < period) { result.push(null); continue }
    let hi = -Infinity, lo = Infinity
    for (let j = i - period + 1; j <= i; j++) {
      hi = Math.max(hi, c[j])
      lo = Math.min(lo, c[j])
    }
    result.push(hi === lo ? 50 : 100 * (c[i] - lo) / (hi - lo))
  }
  return result
}

// ═══════════════════════════════════════════
// 趋势/方向类
// ═══════════════════════════════════════════

/** Aroon 阿隆指标 */
export function calcAroon (klineData, period = 25) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < period) { result.push({ up: null, down: null }); continue }
    let highIdx = 0
    let lowIdx = 0
    let highest = -Infinity
    let lowest = Infinity
    for (let j = 0; j <= period; j++) {
      const idx = i - period + j
      if (klineData[idx].high > highest) { highest = klineData[idx].high; highIdx = j }
      if (klineData[idx].low < lowest) { lowest = klineData[idx].low; lowIdx = j }
    }
    result.push({
      up: 100 * highIdx / period,
      down: 100 * lowIdx / period
    })
  }
  return result
}

/** Aroon Oscillator */
export function calcAroonOsc (klineData, period = 25) {
  const aroon = calcAroon(klineData, period)
  return aroon.map(v => {
    if (v.up == null || v.down == null) return null
    return v.up - v.down
  })
}

/** VI - 涡旋指标 */
export function calcVI (klineData, period = 14) {
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (i < period) { result.push({ viPlus: null, viMinus: null }); continue }
    let vmPlus = 0
    let vmMinus = 0
    let tr = 0
    for (let j = i - period + 1; j <= i; j++) {
      vmPlus += Math.abs(klineData[j].high - klineData[j - 1].low)
      vmMinus += Math.abs(klineData[j].low - klineData[j - 1].high)
      const hl = klineData[j].high - klineData[j].low
      const hc = Math.abs(klineData[j].high - klineData[j - 1].close)
      const lc = Math.abs(klineData[j].low - klineData[j - 1].close)
      tr += Math.max(hl, hc, lc)
    }
    result.push({
      viPlus: tr === 0 ? 0 : vmPlus / tr,
      viMinus: tr === 0 ? 0 : vmMinus / tr
    })
  }
  return result
}

// ═══════════════════════════════════════════
// 波动率类
// ═══════════════════════════════════════════

/** ATR (已有，这里导出) */
export function calcATR (klineData, period = 14) {
  const tr = []
  for (let i = 0; i < klineData.length; i++) {
    if (i === 0) {
      tr.push(klineData[i].high - klineData[i].low)
    } else {
      const hl = klineData[i].high - klineData[i].low
      const hc = Math.abs(klineData[i].high - klineData[i - 1].close)
      const lc = Math.abs(klineData[i].low - klineData[i - 1].close)
      tr.push(Math.max(hl, hc, lc))
    }
  }
  return _sma(tr, period)
}

/** BBWidth - 布林带宽 */
export function calcBBWidth (klineData, period = 20, mult = 2) {
  const c = closes(klineData)
  const smaArr = _sma(c, period)
  const result = []
  for (let i = 0; i < c.length; i++) {
    if (smaArr[i] == null) { result.push(null); continue }
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) {
      sum += (c[j] - smaArr[i]) * (c[j] - smaArr[i])
    }
    const std = Math.sqrt(sum / period)
    const upper = smaArr[i] + mult * std
    const lower = smaArr[i] - mult * std
    result.push(smaArr[i] === 0 ? 0 : (upper - lower) / smaArr[i] * 100)
  }
  return result
}

/** BB%B - 布林%B */
export function calcBBPct (klineData, period = 20, mult = 2) {
  const c = closes(klineData)
  const smaArr = _sma(c, period)
  const result = []
  for (let i = 0; i < c.length; i++) {
    if (smaArr[i] == null) { result.push(null); continue }
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) {
      sum += (c[j] - smaArr[i]) * (c[j] - smaArr[i])
    }
    const std = Math.sqrt(sum / period)
    const upper = smaArr[i] + mult * std
    const lower = smaArr[i] - mult * std
    result.push(upper === lower ? 0.5 : (c[i] - lower) / (upper - lower))
  }
  return result
}

/** Mass Index 梅斯线 */
export function calcMassIndex (klineData, period = 9, sumPeriod = 25) {
  const hl = klineData.map(d => d.high - d.low)
  const ema1 = _ema(hl, period)
  const ema2 = _ema(ema1.map(v => v ?? 0), period)
  const ratio = ema1.map((v, i) => {
    if (v == null || ema2[i] == null || ema2[i] === 0) return null
    return v / ema2[i]
  })
  return _sma(ratio.map(v => v ?? 0), sumPeriod)
}

// ═══════════════════════════════════════════
// 成交量类
// ═══════════════════════════════════════════

/** VWAP - 成交量加权均价 */
export function calcVWAP (klineData) {
  let cumPV = 0
  let cumV = 0
  return klineData.map(d => {
    const tp = (d.high + d.low + d.close) / 3
    const vol = d.volume || 0
    cumPV += tp * vol
    cumV += vol
    return cumV === 0 ? null : cumPV / cumV
  })
}

/** CMF - 蔡金资金流 */
export function calcCMF (klineData, period = 20) {
  const mfv = klineData.map(d => {
    const hl = d.high - d.low
    if (hl === 0) return 0
    return ((d.close - d.low) - (d.high - d.close)) / hl * (d.volume || 0)
  })
  const volSum = _sma(klineData.map(d => d.volume || 0), period)
  const mfvSum = _sma(mfv, period)
  return mfvSum.map((v, i) => {
    if (v == null || volSum[i] == null || volSum[i] === 0) return null
    return v / volSum[i]
  })
}

/** FI - 力量指数 */
export function calcFI (klineData, period = 13) {
  const c = closes(klineData)
  const v = volumes(klineData)
  const raw = c.map((val, i) => {
    if (i === 0) return 0
    return (val - c[i - 1]) * (v[i] || 0)
  })
  return _ema(raw, period)
}

/** KVO - 克林格成交量摆动 */
export function calcKVO (klineData, fast = 34, slow = 55, signal = 13) {
  const hlc = klineData.map(d => (d.high + d.low + d.close) / 3)
  const dm = hlc.map((v, i) => i === 0 ? 0 : v - hlc[i - 1])
  const trend = dm.map(v => v >= 0 ? 1 : -1)
  const vol = volumes(klineData)
  const vf = klineData.map((d, i) => {
    const hl = d.high - d.low
    return (vol[i] || 0) * Math.abs(2 * (hl === 0 ? 0 : (d.close - d.low) / hl - 1)) * trend[i] * 100
  })
  const emaF = _ema(vf, fast)
  const emaS = _ema(vf, slow)
  const kvo = emaF.map((v, i) => {
    if (v == null || emaS[i] == null) return null
    return v - emaS[i]
  })
  const signalLine = _ema(kvo.map(v => v ?? 0), signal)
  return kvo.map((v, i) => ({
    kvo: v,
    signal: signalLine[i]
  }))
}

/** PVO - 百分比成交量震荡器 */
export function calcPVO (klineData, fast = 12, slow = 26, signal = 9) {
  const v = volumes(klineData)
  const emaF = _ema(v, fast)
  const emaS = _ema(v, slow)
  const pvo = emaF.map((f, i) => {
    if (f == null || emaS[i] == null || emaS[i] === 0) return null
    return (f - emaS[i]) / emaS[i] * 100
  })
  const signalLine = _ema(pvo.map(v => v ?? 0), signal)
  return pvo.map((v, i) => ({
    pvo: v,
    signal: signalLine[i]
  }))
}

/** NVI - 负量指标 */
export function calcNVI (klineData) {
  let nvi = 1000
  return klineData.map((d, i) => {
    if (i === 0) return nvi
    if (d.volume < klineData[i - 1].volume) {
      const pctChange = (d.close - klineData[i - 1].close) / klineData[i - 1].close * 100
      nvi += pctChange
    }
    return nvi
  })
}

/** PVI - 正量指标 */
export function calcPVI (klineData) {
  let pvi = 1000
  return klineData.map((d, i) => {
    if (i === 0) return pvi
    if (d.volume > klineData[i - 1].volume) {
      const pctChange = (d.close - klineData[i - 1].close) / klineData[i - 1].close * 100
      pvi += pctChange
    }
    return pvi
  })
}

// ═══════════════════════════════════════════
// 超买超卖/震荡类
// ═══════════════════════════════════════════

/** StochRSI - 随机RSI */
export function calcStochRSI (klineData, rsiPeriod = 14, stochPeriod = 14, kSmooth = 3, dSmooth = 3) {
  // 先计算 RSI
  const c = closes(klineData)
  const rsiValues = []
  let avgGain = 0, avgLoss = 0
  for (let i = 0; i < c.length; i++) {
    if (i === 0) { rsiValues.push(null); continue }
    const change = c[i] - c[i - 1]
    const gain = change > 0 ? change : 0
    const loss = change < 0 ? -change : 0
    if (i < rsiPeriod) { rsiValues.push(null); continue }
    if (i === rsiPeriod) {
      let sg = 0, sl = 0
      for (let j = 1; j <= rsiPeriod; j++) {
        const chg = c[j] - c[j - 1]
        if (chg > 0) sg += chg; else sl -= chg
      }
      avgGain = sg / rsiPeriod
      avgLoss = sl / rsiPeriod
    } else {
      avgGain = (avgGain * (rsiPeriod - 1) + gain) / rsiPeriod
      avgLoss = (avgLoss * (rsiPeriod - 1) + loss) / rsiPeriod
    }
    const rs = avgLoss === 0 ? 100 : avgGain / avgLoss
    rsiValues.push(100 - 100 / (1 + rs))
  }
  // StochRSI
  const result = []
  const kValues = []
  for (let i = 0; i < rsiValues.length; i++) {
    if (i < stochPeriod - 1 || rsiValues[i] == null) { result.push({ k: null, d: null }); kValues.push(null); continue }
    let hi = -Infinity, lo = Infinity
    for (let j = i - stochPeriod + 1; j <= i; j++) {
      if (rsiValues[j] != null) {
        hi = Math.max(hi, rsiValues[j])
        lo = Math.min(lo, rsiValues[j])
      }
    }
    const stochRsi = hi === lo ? 0.5 : (rsiValues[i] - lo) / (hi - lo)
    kValues.push(stochRsi * 100)
  }
  // Smooth K and D
  const kSmoothed = _sma(kValues.map(v => v ?? 0), kSmooth)
  const dSmoothed = _sma(kSmoothed.map(v => v ?? 0), dSmooth)
  return kSmoothed.map((k, i) => ({
    k: k,
    d: dSmoothed[i]
  }))
}

/** CMO - 钱德动量摆动指标 */
export function calcCMO (klineData, period = 14) {
  const c = closes(klineData)
  const result = []
  for (let i = 0; i < c.length; i++) {
    if (i < period) { result.push(null); continue }
    let sumUp = 0, sumDown = 0
    for (let j = i - period + 1; j <= i; j++) {
      const change = c[j] - c[j - 1]
      if (change > 0) sumUp += change
      else sumDown -= change
    }
    result.push(sumUp + sumDown === 0 ? 0 : (sumUp - sumDown) / (sumUp + sumDown) * 100)
  }
  return result
}

// ═══════════════════════════════════════════
// 震荡/杂项类
// ═══════════════════════════════════════════

/** Chandelier Exit 吊灯止损 */
export function calcChandelier (klineData, period = 22, multiplier = 3) {
  const atrArr = calcATR(klineData, period)
  const result = []
  for (let i = 0; i < klineData.length; i++) {
    if (atrArr[i] == null) { result.push({ long: null, short: null }); continue }
    let hi = -Infinity, lo = Infinity
    for (let j = Math.max(0, i - period + 1); j <= i; j++) {
      hi = Math.max(hi, klineData[j].high)
      lo = Math.min(lo, klineData[j].low)
    }
    result.push({
      long: hi - multiplier * atrArr[i],
      short: lo + multiplier * atrArr[i]
    })
  }
  return result
}

/** Elder Ray 艾达透视指标 */
export function calcElderRay (klineData, period = 13) {
  const c = closes(klineData)
  const emaArr = _ema(c, period)
  return klineData.map((d, i) => {
    if (emaArr[i] == null) return { bullPower: null, bearPower: null }
    return {
      bullPower: d.high - emaArr[i],
      bearPower: d.low - emaArr[i]
    }
  })
}

// ═══════════════════════════════════════════
// PPO - 百分比价格震荡器
// ═══════════════════════════════════════════
export function calcPPO (klineData, fast = 12, slow = 26, signal = 9) {
  const c = closes(klineData)
  const emaF = _ema(c, fast)
  const emaS = _ema(c, slow)
  const ppo = emaF.map((f, i) => {
    if (f == null || emaS[i] == null || emaS[i] === 0) return null
    return (f - emaS[i]) / emaS[i] * 100
  })
  const signalLine = _ema(ppo.map(v => v ?? 0), signal)
  const histogram = ppo.map((v, i) => {
    if (v == null || signalLine[i] == null) return null
    return v - signalLine[i]
  })
  return ppo.map((v, i) => ({
    ppo: v,
    signal: signalLine[i],
    histogram: histogram[i]
  }))
}

// ═══════════════════════════════════════════
// 指标定义注册表
// ═══════════════════════════════════════════
/**
 * 每个指标定义：
 * - id: 指标 ID
 * - calc: 计算函数 (klineData, params) => result[]
 * - defaultParams: 默认参数
 * - figures: figure 定义数组 [{ key, title, type, color, styles? }]
 * - type: 'line' | 'macd' | 'band' | 'multi' | 'bar'
 */
export const INDICATOR_REGISTRY = {
  // ── 移动平均类 (主图) ──
  wma: { calc: (d, p) => calcWMA(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'wma', title: 'WMA', type: 'line' }], type: 'line' },
  dema: { calc: (d, p) => calcDEMA(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'dema', title: 'DEMA', type: 'line' }], type: 'line' },
  tema: { calc: (d, p) => calcTEMA(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'tema', title: 'TEMA', type: 'line' }], type: 'line' },
  hma: { calc: (d, p) => calcHMA(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'hma', title: 'HMA', type: 'line' }], type: 'line' },
  kama: { calc: (d, p) => calcKAMA(d, p.length), defaultParams: { length: 10 }, figures: [{ key: 'kama', title: 'KAMA', type: 'line' }], type: 'line' },
  alma: { calc: (d, p) => calcALMA(d, p.length), defaultParams: { length: 9 }, figures: [{ key: 'alma', title: 'ALMA', type: 'line' }], type: 'line' },
  vwma: { calc: (d, p) => calcVWMA(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'vwma', title: 'VWMA', type: 'line' }], type: 'line' },

  // ── 通道/包络类 (主图) ──
  kc: { calc: (d, p) => calcKC(d, p.length, p.mult), defaultParams: { length: 20, mult: 1.5 }, figures: [{ key: 'upper', title: 'Upper', type: 'line' }, { key: 'middle', title: 'Middle', type: 'line' }, { key: 'lower', title: 'Lower', type: 'line' }], type: 'band' },
  dc: { calc: (d, p) => calcDC(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'upper', title: 'Upper', type: 'line' }, { key: 'middle', title: 'Middle', type: 'line' }, { key: 'lower', title: 'Lower', type: 'line' }], type: 'band' },
  env: { calc: (d, p) => calcENV(d, p.length, p.pct), defaultParams: { length: 20, pct: 2.5 }, figures: [{ key: 'upper', title: 'Upper', type: 'line' }, { key: 'middle', title: 'Middle', type: 'line' }, { key: 'lower', title: 'Lower', type: 'line' }], type: 'band' },

  // ── 趋势/止损类 (主图) ──
  supertrend: { calc: (d, p) => calcSuperTrend(d, p.length, p.mult), defaultParams: { length: 10, mult: 3 }, figures: [{ key: 'st', title: 'ST', type: 'line' }], type: 'line' },
  vstop: { calc: (d, p) => calcVStop(d, p.length, p.mult), defaultParams: { length: 20, mult: 2 }, figures: [{ key: 'vstop', title: 'VSTOP', type: 'line' }], type: 'line' },

  // ── Ichimoku (主图) ──
  ichimoku: { calc: (d, p) => calcIchimoku(d, p.tenkan, p.kijun, p.senkou), defaultParams: { tenkan: 9, kijun: 26, senkou: 52 }, figures: [{ key: 'tenkan', title: 'Tenkan', type: 'line' }, { key: 'kijun', title: 'Kijun', type: 'line' }, { key: 'senkouA', title: 'SenkouA', type: 'line' }, { key: 'senkouB', title: 'SenkouB', type: 'line' }], type: 'multi' },

  // ── 结构分析类 (主图) ──
  zigzag: { calc: (d, p) => calcZigZag(d, p.dev), defaultParams: { dev: 5 }, figures: [{ key: 'zz', title: 'ZZ', type: 'line' }], type: 'line' },
  pivot_hi: { calc: (d, p) => calcPivotHi(d, p.left, p.right), defaultParams: { left: 5, right: 5 }, figures: [{ key: 'ph', title: 'PH', type: 'circle' }], type: 'line' },
  pivot_lo: { calc: (d, p) => calcPivotLo(d, p.left, p.right), defaultParams: { left: 5, right: 5 }, figures: [{ key: 'pl', title: 'PL', type: 'circle' }], type: 'line' },

  // ── 动量类 (副图) ──
  mom: { calc: (d, p) => calcMOM(d, p.length), defaultParams: { length: 10 }, figures: [{ key: 'mom', title: 'MOM', type: 'line' }], type: 'line' },
  tsi: { calc: (d, p) => calcTSI(d, p.long, p.short), defaultParams: { long: 25, short: 13 }, figures: [{ key: 'tsi', title: 'TSI', type: 'line' }], type: 'line' },
  bop: { calc: (d) => calcBOP(d), defaultParams: {}, figures: [{ key: 'bop', title: 'BOP', type: 'line' }], type: 'line' },
  rmi: { calc: (d, p) => calcRMI(d, p.length, p.mom), defaultParams: { length: 14, mom: 5 }, figures: [{ key: 'rmi', title: 'RMI', type: 'line' }], type: 'line' },
  kst: { calc: (d, p) => calcKST(d, p.r1, p.r2, p.r3, p.r4), defaultParams: { r1: 10, r2: 15, r3: 20, r4: 30 }, figures: [{ key: 'kst', title: 'KST', type: 'line' }, { key: 'signal', title: 'Signal', type: 'line' }], type: 'multi' },
  uo: { calc: (d, p) => calcUO(d, p.s1, p.s2, p.s3), defaultParams: { s1: 7, s2: 14, s3: 28 }, figures: [{ key: 'uo', title: 'UO', type: 'line' }], type: 'line' },
  coppock: { calc: (d, p) => calcCoppock(d, p.wma, p.roc1, p.roc2), defaultParams: { wma: 10, roc1: 14, roc2: 11 }, figures: [{ key: 'copp', title: 'COPP', type: 'line' }], type: 'line' },

  // ── Bill Williams 系列 (副图) ──
  ac: { calc: (d) => calcAC(d), defaultParams: {}, figures: [{ key: 'ac', title: 'AC', type: 'bar' }], type: 'bar' },
  alligator: { calc: (d) => calcAlligator(d), defaultParams: {}, figures: [{ key: 'jaw', title: 'Jaw', type: 'line' }, { key: 'teeth', title: 'Teeth', type: 'line' }, { key: 'lips', title: 'Lips', type: 'line' }], type: 'multi' },

  // ── Ehlers 系列 (副图) ──
  fisher: { calc: (d, p) => calcFisher(d, p.length), defaultParams: { length: 10 }, figures: [{ key: 'fisher', title: 'Fisher', type: 'line' }, { key: 'signal', title: 'Signal', type: 'line' }], type: 'multi' },
  e_stoch: { calc: (d, p) => calcEhlersStoch(d, p.length), defaultParams: { length: 10 }, figures: [{ key: 'estoch', title: 'ESTOCH', type: 'line' }], type: 'line' },

  // ── 趋势/方向类 (副图) ──
  aroon: { calc: (d, p) => calcAroon(d, p.length), defaultParams: { length: 25 }, figures: [{ key: 'up', title: 'Up', type: 'line' }, { key: 'down', title: 'Down', type: 'line' }], type: 'multi' },
  aroonosc: { calc: (d, p) => calcAroonOsc(d, p.length), defaultParams: { length: 25 }, figures: [{ key: 'aroonosc', title: 'AROONOSC', type: 'line' }], type: 'line' },
  vi: { calc: (d, p) => calcVI(d, p.length), defaultParams: { length: 14 }, figures: [{ key: 'viPlus', title: 'VI+', type: 'line' }, { key: 'viMinus', title: 'VI-', type: 'line' }], type: 'multi' },

  // ── 波动率类 (副图) ──
  bbwidth: { calc: (d, p) => calcBBWidth(d, p.length, p.mult), defaultParams: { length: 20, mult: 2 }, figures: [{ key: 'bbw', title: 'BBW', type: 'line' }], type: 'line' },
  bbpct: { calc: (d, p) => calcBBPct(d, p.length, p.mult), defaultParams: { length: 20, mult: 2 }, figures: [{ key: 'bbpct', title: '%B', type: 'line' }], type: 'line' },
  massidx: { calc: (d, p) => calcMassIndex(d, p.length, p.sum), defaultParams: { length: 9, sum: 25 }, figures: [{ key: 'mi', title: 'MI', type: 'line' }], type: 'line' },

  // ── 成交量类 (副图) ──
  vwap: { calc: (d) => calcVWAP(d), defaultParams: {}, figures: [{ key: 'vwap', title: 'VWAP', type: 'line' }], type: 'line' },
  cmf: { calc: (d, p) => calcCMF(d, p.length), defaultParams: { length: 20 }, figures: [{ key: 'cmf', title: 'CMF', type: 'line' }], type: 'line' },
  fi: { calc: (d, p) => calcFI(d, p.length), defaultParams: { length: 13 }, figures: [{ key: 'fi', title: 'FI', type: 'line' }], type: 'line' },
  kvo: { calc: (d, p) => calcKVO(d, p.fast, p.slow, p.signal), defaultParams: { fast: 34, slow: 55, signal: 13 }, figures: [{ key: 'kvo', title: 'KVO', type: 'line' }, { key: 'signal', title: 'Signal', type: 'line' }], type: 'multi' },
  pvo: { calc: (d, p) => calcPVO(d, p.fast, p.slow, p.signal), defaultParams: { fast: 12, slow: 26, signal: 9 }, figures: [{ key: 'pvo', title: 'PVO', type: 'line' }, { key: 'signal', title: 'Signal', type: 'line' }], type: 'multi' },
  nvi: { calc: (d) => calcNVI(d), defaultParams: {}, figures: [{ key: 'nvi', title: 'NVI', type: 'line' }], type: 'line' },
  pvi: { calc: (d) => calcPVI(d), defaultParams: {}, figures: [{ key: 'pvi', title: 'PVI', type: 'line' }], type: 'line' },

  // ── 超买超卖类 (副图) ──
  stochrsi: { calc: (d, p) => calcStochRSI(d, p.rsiPeriod, p.stochPeriod, p.kSmooth, p.dSmooth), defaultParams: { rsiPeriod: 14, stochPeriod: 14, kSmooth: 3, dSmooth: 3 }, figures: [{ key: 'k', title: 'K', type: 'line' }, { key: 'd', title: 'D', type: 'line' }], type: 'multi' },
  cmo: { calc: (d, p) => calcCMO(d, p.length), defaultParams: { length: 14 }, figures: [{ key: 'cmo', title: 'CMO', type: 'line' }], type: 'line' },

  // ── MACD 系列 (副图) ──
  ppo: { calc: (d, p) => calcPPO(d, p.fast, p.slow, p.signal), defaultParams: { fast: 12, slow: 26, signal: 9 }, figures: [{ key: 'ppo', title: 'PPO', type: 'line' }, { key: 'signal', title: 'Signal', type: 'line' }, { key: 'histogram', title: 'HIST', type: 'bar' }], type: 'macd' },

  // ── 震荡/杂项类 (副图) ──
  chandelier: { calc: (d, p) => calcChandelier(d, p.length, p.mult), defaultParams: { length: 22, mult: 3 }, figures: [{ key: 'long', title: 'Long', type: 'line' }, { key: 'short', title: 'Short', type: 'line' }], type: 'multi' },
  elder_ray: { calc: (d, p) => calcElderRay(d, p.length), defaultParams: { length: 13 }, figures: [{ key: 'bullPower', title: 'Bull', type: 'line' }, { key: 'bearPower', title: 'Bear', type: 'line' }], type: 'multi' },

  // ── 枢轴点系列 (主图) ──
  pivot_classic: { calc: (d) => calcPivotClassic(d), defaultParams: {}, figures: [{ key: 'pp', title: 'PP', type: 'line' }, { key: 'r1', title: 'R1', type: 'line' }, { key: 'r2', title: 'R2', type: 'line' }, { key: 's1', title: 'S1', type: 'line' }, { key: 's2', title: 'S2', type: 'line' }], type: 'multi' }
}
