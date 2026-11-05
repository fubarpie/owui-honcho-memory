# Honcho v3 API summary (generated)

POST /v3/workspaces
  query: []
  body: {id*: string, metadata: object, configuration: {custom_instructions: string | null, reasoning: object | null, peer_card: object | null, summary: object | null, dream: object | null, dialectic: object | null}}
POST /v3/workspaces/list
  query: ['reverse', 'page', 'size', 'cursor']
  body: {filters: object | null} | null
PUT /v3/workspaces/{workspace_id}
  query: []
  body: {metadata: object | null, configuration: {custom_instructions: string | null, reasoning: object | null, peer_card: object | null, summary: object | null, dream: object | null, dialectic: object | null} | null}
DELETE /v3/workspaces/{workspace_id}
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/search
  query: []
  body: {query*: string, filters: object | null, limit: integer, scope: string | null}
GET /v3/workspaces/{workspace_id}/queue/status
  query: ['observer_id', 'sender_id', 'session_id']
  body: None
POST /v3/workspaces/{workspace_id}/schedule_dream
  query: []
  body: {observer*: string, observed: string | null, dream_type*: string, session_id: string | null, rebuild: boolean}
POST /v3/workspaces/{workspace_id}/chat
  query: []
  body: {session_id: string | null, query*: string, stream: boolean, reasoning_level: string, response_format: object | null, scope: string | [string] | null, include_evidence: boolean}
POST /v3/workspaces/{workspace_id}/peers/list
  query: ['reverse', 'page', 'size', 'cursor']
  body: {filters: object | null, kind: string | null} | null
POST /v3/workspaces/{workspace_id}/peers
  query: []
  body: {id*: string, metadata: object | null, configuration: object | null}
PUT /v3/workspaces/{workspace_id}/peers/{peer_id}
  query: []
  body: {metadata: object | null, configuration: object | null}
POST /v3/workspaces/{workspace_id}/peers/{peer_id}/sessions
  query: ['reverse', 'page', 'size', 'cursor']
  body: {filters: object | null} | null
POST /v3/workspaces/{workspace_id}/peers/{peer_id}/chat
  query: []
  body: {session_id: string | null, filters: object | null, scope: string | [string] | null, target: string | null, query*: string, stream: boolean, reasoning_level: string, response_format: object | null, include_evidence: boolean}
POST /v3/workspaces/{workspace_id}/peers/{peer_id}/representation
  query: []
  body: {session_id: string | null, filters: object | null, scope: string | [string] | null, target: string | null, search_query: string | null, search_top_k: integer | null, search_max_distance: number | null, include_most_frequent: boolean | null, max_conclusions: integer | null}
GET /v3/workspaces/{workspace_id}/peers/{peer_id}/card
  query: ['target']
  body: None
PUT /v3/workspaces/{workspace_id}/peers/{peer_id}/card
  query: ['target']
  body: {peer_card*: [string]}
GET /v3/workspaces/{workspace_id}/peers/{peer_id}/context
  query: ['target', 'search_query', 'search_top_k', 'search_max_distance', 'include_most_frequent', 'max_conclusions']
  body: None
POST /v3/workspaces/{workspace_id}/peers/{peer_id}/search
  query: []
  body: {query*: string, filters: object | null, limit: integer}
POST /v3/workspaces/{workspace_id}/sessions/list
  query: ['reverse', 'include_scopes', 'page', 'size', 'cursor']
  body: {filters: object | null, scope: string | null, in_no_scope: boolean} | null
POST /v3/workspaces/{workspace_id}/sessions
  query: []
  body: {id*: string, metadata: object | null, peers: object | null, configuration: {custom_instructions: string | null, reasoning: object | null, peer_card: object | null, summary: object | null, dream: object | null, dialectic: object | null} | null, scopes: [string] | null}
PUT /v3/workspaces/{workspace_id}/sessions/{session_id}
  query: []
  body: {metadata: object | null, configuration: {custom_instructions: string | null, reasoning: object | null, peer_card: object | null, summary: object | null, dream: object | null, dialectic: object | null} | null}
DELETE /v3/workspaces/{workspace_id}/sessions/{session_id}
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/sessions/{session_id}/clone
  query: ['message_id']
  body: None
POST /v3/workspaces/{workspace_id}/sessions/{session_id}/peers
  query: []
  body: object
PUT /v3/workspaces/{workspace_id}/sessions/{session_id}/peers
  query: []
  body: object
DELETE /v3/workspaces/{workspace_id}/sessions/{session_id}/peers
  query: []
  body: [string]
GET /v3/workspaces/{workspace_id}/sessions/{session_id}/peers
  query: ['page', 'size', 'cursor']
  body: None
GET /v3/workspaces/{workspace_id}/sessions/{session_id}/peers/{peer_id}/config
  query: []
  body: None
PUT /v3/workspaces/{workspace_id}/sessions/{session_id}/peers/{peer_id}/config
  query: []
  body: {observe_me: boolean | null, observe_others: boolean | null}
GET /v3/workspaces/{workspace_id}/sessions/{session_id}/context
  query: ['tokens', 'search_query', 'summary', 'peer_target', 'peer_perspective', 'scope', 'sessions', 'limit_to_session', 'search_top_k', 'search_max_distance', 'include_most_frequent', 'max_conclusions']
  body: None
GET /v3/workspaces/{workspace_id}/sessions/{session_id}/summaries
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/sessions/{session_id}/search
  query: []
  body: {query*: string, filters: object | null, limit: integer}
POST /v3/workspaces/{workspace_id}/scopes
  query: []
  body: {id*: string, metadata: object | null}
POST /v3/workspaces/{workspace_id}/scopes/list
  query: ['reverse', 'include_session_count', 'page', 'size', 'cursor']
  body: {filters: object | null} | null
GET /v3/workspaces/{workspace_id}/scopes/{scope_id}
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/scopes/{scope_id}/sessions
  query: []
  body: {session_ids*: [string]}
DELETE /v3/workspaces/{workspace_id}/scopes/{scope_id}/sessions/{session_id}
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/scopes/{scope_id}/sessions/remove
  query: []
  body: {session_ids*: [string]}
POST /v3/workspaces/{workspace_id}/scopes/{scope_id}/sessions/list
  query: ['reverse', 'page', 'size', 'cursor']
  body: None
GET /v3/workspaces/{workspace_id}/scopes/{scope_id}/status
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/sessions/{session_id}/messages
  query: []
  body: {messages*: [{content*: string, peer_id*: string, metadata: object | null, configuration: object | null, created_at: string | null}]}
POST /v3/workspaces/{workspace_id}/sessions/{session_id}/messages/upload
  query: []
  body: None
POST /v3/workspaces/{workspace_id}/sessions/{session_id}/messages/list
  query: ['reverse', 'page', 'size', 'cursor']
  body: {filters: object | null} | null
GET /v3/workspaces/{workspace_id}/sessions/{session_id}/messages/{message_id}
  query: []
  body: None
PUT /v3/workspaces/{workspace_id}/sessions/{session_id}/messages/{message_id}
  query: []
  body: {metadata: object | null}
POST /v3/workspaces/{workspace_id}/conclusions
  query: []
  body: {conclusions*: [{content*: string, observer_id*: string, observed_id*: string, session_id: string | null}]}
POST /v3/workspaces/{workspace_id}/conclusions/list
  query: ['reverse', 'page', 'size', 'cursor']
  body: {filters: object | null} | null
POST /v3/workspaces/{workspace_id}/conclusions/query
  query: []
  body: {query*: string, top_k: integer, distance: number | null, filters: object | null}
GET /v3/workspaces/{workspace_id}/conclusions/{conclusion_id}
  query: []
  body: None
DELETE /v3/workspaces/{workspace_id}/conclusions/{conclusion_id}
  query: []
  body: None
POST /v3/keys
  query: ['workspace_id', 'peer_id', 'session_id', 'expires_at', 'scope_id']
  body: None
POST /v3/workspaces/{workspace_id}/webhooks
  query: []
  body: {url*: string}
GET /v3/workspaces/{workspace_id}/webhooks
  query: ['page', 'size', 'cursor']
  body: None
DELETE /v3/workspaces/{workspace_id}/webhooks/{endpoint_id}
  query: []
  body: None
GET /v3/workspaces/{workspace_id}/webhooks/test
  query: []
  body: None
GET /health
  query: []
  body: None