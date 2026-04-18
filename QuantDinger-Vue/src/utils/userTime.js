import storage from 'store'
import { USER_INFO } from '@/store/mutation-types'

export function getUserTimezoneFromStorage () {
  try {
    const u = storage.get(USER_INFO) || {}
    const tz = u.timezone
    return (tz && String(tz).trim()) || ''
  } catch (e) {
    return ''
  }
}

export function parseToDate (input) {
  if (input == null || input === '') return null
  if (input instanceof Date) {
    return isNaN(input.getTime()) ? null : input
  }
  if (typeof input === 'number') {
    const ms = input < 1e12 ? input * 1000 : input
    const d = new Date(ms)
    return isNaN(d.getTime()) ? null : d
  }
  if (typeof input === 'string') {
    const s = input.trim()
    if (/^\d+$/.test(s)) {
      const n = parseInt(s, 10)
      const ms = n < 1e12 ? n * 1000 : n
      const d = new Date(ms)
      return isNaN(d.getTime()) ? null : d
    }
    const d = new Date(s)
    return isNaN(d.getTime()) ? null : d
  }
  return null
}

/**
 * Format instant for display using profile timezone when set; otherwise browser default.
 */
export function formatUserDateTime (input, opts = {}) {
  const d = parseToDate(input)
  if (!d) return opts.fallback != null ? opts.fallback : '-'
  const locale = opts.locale || (typeof navigator !== 'undefined' && navigator.language) || 'zh-CN'
  const tz = getUserTimezoneFromStorage()
  const intlOpts = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }
  if (tz) {
    intlOpts.timeZone = tz
  }
  try {
    return d.toLocaleString(locale, intlOpts)
  } catch (e) {
    try {
      const { timeZone, ...rest } = intlOpts
      return d.toLocaleString(locale, rest)
    } catch (e2) {
      return d.toLocaleString()
    }
  }
}

/**
 * Format instant in the browser's local timezone (ignores profile timezone override).
 * Use for audit-style timestamps (e.g. trade history) so wall clock matches the user's machine.
 */
export function formatBrowserLocalDateTime (input, opts = {}) {
  const d = parseToDate(input)
  if (!d) return opts.fallback != null ? opts.fallback : '-'
  const locale = opts.locale || (typeof navigator !== 'undefined' && navigator.language) || 'zh-CN'
  const intlOpts = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }
  try {
    return d.toLocaleString(locale, intlOpts)
  } catch (e) {
    return d.toLocaleString()
  }
}
