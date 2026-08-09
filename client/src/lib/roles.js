/** Role constants, kept out of the provider module so fast refresh works. */

/** Where each role lands after signing in. */
export const HOME_FOR_ROLE = {
  CUSTOMER: '/accounts',
  FRAUD_ANALYST: '/fraud/queue',
  OPS: '/ops/queues',
  ADMIN: '/admin/rules',
}

export const ROLE_LABEL = {
  CUSTOMER: 'Personal Banking',
  FRAUD_ANALYST: 'Fraud Operations',
  OPS: 'Service Operations',
  ADMIN: 'Administration',
}

export const STAFF_ROLES = ['FRAUD_ANALYST', 'OPS', 'ADMIN']
