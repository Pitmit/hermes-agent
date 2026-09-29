import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

import type { KanbanActivityResponse } from '../gatewayTypes.js'
import { type KanbanActivityModel, normalizeKanbanActivity } from '../lib/kanbanActivity.js'

const EMPTY: KanbanActivityResponse = {
  active_count: 0,
  attention_count: 0,
  boards: [],
  checked_at: 0,
  diagnostics: []
}

export const $kanbanActivity = atom<KanbanActivityModel>(normalizeKanbanActivity(EMPTY, 0))

export function applyKanbanActivity(payload: KanbanActivityResponse = EMPTY) {
  const next = normalizeKanbanActivity(payload)

  if (JSON.stringify($kanbanActivity.get()) !== JSON.stringify(next)) {
    $kanbanActivity.set(next)
  }
}

export function useKanbanActivity() {
  return useStore($kanbanActivity)
}
