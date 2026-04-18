import request from '@/utils/request'

const api = {
  list: '/api/credentials/list',
  get: '/api/credentials/get',
  create: '/api/credentials/create',
  delete: '/api/credentials/delete',
  egressIp: '/api/credentials/egress-ip'
}

export function listExchangeCredentials (params = {}) {
  return request({
    url: api.list,
    method: 'get',
    params
  })
}

export function getExchangeCredential (id, params = {}) {
  return request({
    url: api.get,
    method: 'get',
    params: { id, ...params }
  })
}

export function createExchangeCredential (data) {
  return request({
    url: api.create,
    method: 'post',
    data
  })
}

export function deleteExchangeCredential (id, params = {}) {
  return request({
    url: api.delete,
    method: 'delete',
    params: { id, ...params }
  })
}

/** Server egress IP (for exchange API key IP whitelist). */
export function getCredentialsEgressIp () {
  return request({
    url: api.egressIp,
    method: 'get'
  })
}
