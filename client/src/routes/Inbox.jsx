import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Page } from '../components/AppShell'
import { Button, Empty, ErrorBanner, Loading, Panel, Stamp } from '../components/kit'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/format'

/** In-app notifications. Polled, because there is no WebSocket layer. */

export default function Inbox() {
  const queryClient = useQueryClient()

  const notifications = useQuery({
    queryKey: ['notifications'],
    queryFn: () => api.get('/api/notifications'),
    refetchInterval: 20_000,
  })

  const markRead = useMutation({
    mutationFn: (id) => api.post(`/api/notifications/${id}/read`, {}),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['notifications'] }),
  })

  const rows = notifications.data?.results ?? []
  const unread = notifications.data?.unread_count ?? 0

  return (
    <Page
      title="Inbox"
      subtitle={unread > 0 ? `${unread} unread` : 'Everything read.'}
    >
      <div className="stack" style={{ '--gap': 'var(--s-4)' }}>
        <ErrorBanner error={notifications.error} title="Could not load your inbox" />

        {notifications.isLoading ? (
          <Panel><Loading rows={5} /></Panel>
        ) : rows.length === 0 ? (
          <Panel>
            <Empty mark="[ ✉ ]" title="Nothing here">
              We will write to you when something needs your attention.
            </Empty>
          </Panel>
        ) : (
          rows.map((item) => (
            <Panel
              key={item.id}
              tone={item.read ? 'default' : 'raised'}
              ticks={!item.read}
              className="reveal"
            >
              <div className="row row--between" style={{ alignItems: 'flex-start' }}>
                <div style={{ minWidth: 0 }}>
                  <div className="row" style={{ '--gap': 'var(--s-2)' }}>
                    <strong>{item.subject}</strong>
                    {!item.read && <Stamp tone="live">NEW</Stamp>}
                  </div>
                  <p className="small muted" style={{ marginTop: 6 }}>
                    {item.body}
                  </p>
                  <div className="tiny faint" style={{ marginTop: 8 }}>
                    <span className="mono">{item.template_code}</span> ·{' '}
                    {formatDateTime(item.created_at)}
                  </div>
                </div>
                {!item.read && (
                  <Button size="sm" variant="ghost" onClick={() => markRead.mutate(item.id)}>
                    Mark read
                  </Button>
                )}
              </div>
            </Panel>
          ))
        )}
      </div>
    </Page>
  )
}
