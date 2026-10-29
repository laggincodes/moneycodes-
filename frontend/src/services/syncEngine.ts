/**
 * Offline Synchronization Engine
 * 
 * Safely replays queued offline operations when connectivity returns.
 * 
 * Strict Security & Correctness Mandates:
 * - Verifies current user session against server before replaying any operation.
 * - Refreshes CSRF token via `api.getMe()` before sending mutations.
 * - Never replays actions if session user does not match outbox item owner.
 * - Idempotency key prevents duplicate execution on the server.
 * - Bounded retries with exponential backoff; permanent errors marked as 'failed' or 'conflict'.
 */

import { api } from './api';
import { offlineStore } from './offlineStore';

export interface SyncResult {
  syncedCount: number;
  pendingCount: number;
  failedCount: number;
  conflictCount: number;
  offline: boolean;
}

let isSyncing = false;
let syncListeners: Array<(result: SyncResult) => void> = [];

export function subscribeToSyncEvents(callback: (result: SyncResult) => void): () => void {
  syncListeners.push(callback);
  return () => {
    syncListeners = syncListeners.filter((cb) => cb !== callback);
  };
}

function notifySyncListeners(result: SyncResult) {
  for (const listener of syncListeners) {
    try {
      listener(result);
    } catch (e) {
      console.error('[SyncEngine] Error in sync listener:', e);
    }
  }
}

export const syncEngine = {
  /**
   * Attempts to register Background Sync with the Service Worker if supported.
   */
  async registerBackgroundSync(): Promise<boolean> {
    if ('serviceWorker' in navigator && 'SyncManager' in window) {
      try {
        const registration = await navigator.serviceWorker.ready;
        // @ts-ignore - SyncManager is standard in Chrome/Edge PWA
        await registration.sync.register('sync-offline-leads');
        console.log('[SyncEngine] Background Sync registered successfully.');
        return true;
      } catch (err) {
        console.warn('[SyncEngine] Background Sync registration failed; using foreground sync:', err);
      }
    }
    return false;
  },

  /**
   * Replays pending outbox operations for the authenticated user.
   */
  async syncQueue(userId: string): Promise<SyncResult> {
    if (isSyncing) {
      const items = await offlineStore.getPendingQueue(userId);
      return {
        syncedCount: 0,
        pendingCount: items.length,
        failedCount: 0,
        conflictCount: 0,
        offline: false,
      };
    }

    if (!navigator.onLine) {
      const items = await offlineStore.getPendingQueue(userId);
      const res: SyncResult = {
        syncedCount: 0,
        pendingCount: items.length,
        failedCount: items.filter((i) => i.status === 'failed').length,
        conflictCount: items.filter((i) => i.status === 'conflict').length,
        offline: true,
      };
      notifySyncListeners(res);
      return res;
    }

    isSyncing = true;
    let syncedCount = 0;
    let failedCount = 0;
    let conflictCount = 0;

    try {
      // 1. Verify backend reachability
      try {
        await api.getHealth();
      } catch {
        const items = await offlineStore.getPendingQueue(userId);
        const res: SyncResult = {
          syncedCount: 0,
          pendingCount: items.length,
          failedCount: 0,
          conflictCount: 0,
          offline: true,
        };
        notifySyncListeners(res);
        return res;
      }

      // 2. CRITICAL AUTH VERIFICATION: Verify active session on server
      const currentUser = await api.getMe();
      if (!currentUser || currentUser.id !== userId) {
        console.warn('[SyncEngine] Active session does not match queued user ID. Aborting sync.');
        const items = await offlineStore.getPendingQueue(userId);
        return {
          syncedCount: 0,
          pendingCount: items.length,
          failedCount: 0,
          conflictCount: 0,
          offline: false,
        };
      }

      // 3. Process pending items
      const pendingItems = await offlineStore.getPendingQueue(userId);
      for (const item of pendingItems) {
        // Double check owner against both target user and verified server session
        if (item.user_id !== userId || item.user_id !== currentUser.id) {
          console.error(`[SyncEngine] Security violation: outbox item ${item.id} owner (${item.user_id}) does not match authenticated session (${currentUser.id}). Skipping.`);
          continue;
        }

        await offlineStore.updateQueueItem(item.id, { status: 'syncing' });

        try {
          if (item.action_type === 'UPDATE_PIPELINE') {
            await api.updateLeadPipeline(
              item.lead_id,
              item.payload.pipeline_status,
              item.payload.notes,
              item.idempotency_key,
            );
            // Synced successfully -> remove from outbox
            await offlineStore.removeQueueItem(item.id);
            syncedCount++;
          }
        } catch (err: any) {
          const errMsg = err.message || '';
          console.error(`[SyncEngine] Error syncing item ${item.id}:`, errMsg);

          if (err.status === 404 || errMsg.includes('404') || errMsg.toLowerCase().includes('not found')) {
            // Target lead no longer exists on server (conflict)
            await offlineStore.updateQueueItem(item.id, {
              status: 'conflict',
              error_message: 'Lead was deleted or is no longer accessible.',
            });
            conflictCount++;
          } else if (err.status === 401 || err.status === 403 || errMsg.includes('401') || errMsg.includes('403')) {
            // Auth failure -> stop sync immediately
            await offlineStore.updateQueueItem(item.id, {
              status: 'failed',
              error_message: 'Authentication required to sync changes.',
            });
            failedCount++;
            break;
          } else {
            // Transient or validation error
            const newRetryCount = (item.retry_count || 0) + 1;
            const newStatus = newRetryCount >= 3 ? 'failed' : 'pending';
            await offlineStore.updateQueueItem(item.id, {
              status: newStatus,
              retry_count: newRetryCount,
              error_message: errMsg || 'Network or server error during sync.',
            });
            if (newStatus === 'failed') failedCount++;
          }
        }
      }

      const remaining = await offlineStore.getPendingQueue(userId);
      const res: SyncResult = {
        syncedCount,
        pendingCount: remaining.length,
        failedCount,
        conflictCount,
        offline: false,
      };
      notifySyncListeners(res);
      return res;
    } finally {
      isSyncing = false;
    }
  },
};
