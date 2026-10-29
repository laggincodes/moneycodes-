/**
 * IndexedDB Offline Storage & Outbox Manager
 * 
 * Strict Security & Isolation Mandates:
 * - Every queued action and cached lead is strictly tagged with `user_id`.
 * - Cross-account data leakage is blocked: queries filter by `user_id`.
 * - On logout or user switch, `clearUserOfflineData(userId)` purges all account data.
 * - Sensitive credentials, cookies, and tokens are NEVER stored in IndexedDB.
 */

import type { Lead, PipelineStatus } from '../types';

export interface OutboxItem {
  id: string;
  user_id: string;
  idempotency_key: string;
  action_type: 'UPDATE_PIPELINE';
  lead_id: string;
  payload: {
    pipeline_status: PipelineStatus;
    notes?: string;
    contacted_at?: string;
  };
  created_at: string;
  retry_count: number;
  status: 'pending' | 'syncing' | 'synced' | 'conflict' | 'failed';
  error_message?: string | null;
}

const DB_NAME = 'websitelead_offline_db';
const DB_VERSION = 1;
const OUTBOX_STORE = 'outbox';
const CACHE_STORE = 'cached_leads';

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) {
      reject(new Error('IndexedDB is not supported in this environment.'));
      return;
    }

    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = (event) => {
      const db = (event.target as IDBOpenDBRequest).result;

      // 1. Outbox store
      if (!db.objectStoreNames.contains(OUTBOX_STORE)) {
        const outboxStore = db.createObjectStore(OUTBOX_STORE, { keyPath: 'id' });
        outboxStore.createIndex('by_user_id', 'user_id', { unique: false });
        outboxStore.createIndex('by_status', 'status', { unique: false });
        outboxStore.createIndex('by_created_at', 'created_at', { unique: false });
      }

      // 2. Account-isolated leads cache store
      if (!db.objectStoreNames.contains(CACHE_STORE)) {
        const cacheStore = db.createObjectStore(CACHE_STORE, { keyPath: 'id' });
        cacheStore.createIndex('by_user_id', 'user_id', { unique: false });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export const offlineStore = {
  /**
   * Queues an offline pipeline status or notes update.
   */
  async queuePipelineUpdate(
    userId: string,
    leadId: string,
    pipelineStatus: PipelineStatus,
    notes?: string,
    contactedAt?: string,
  ): Promise<OutboxItem> {
    const db = await openDatabase();
    const id = `outbox_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`;
    const idempotencyKey = `idem_${userId}_${leadId}_${Date.now()}_${Math.random().toString(36).substring(2, 7)}`;

    const item: OutboxItem = {
      id,
      user_id: userId,
      idempotency_key: idempotencyKey,
      action_type: 'UPDATE_PIPELINE',
      lead_id: leadId,
      payload: {
        pipeline_status: pipelineStatus,
        notes,
        contacted_at: contactedAt,
      },
      created_at: new Date().toISOString(),
      retry_count: 0,
      status: 'pending',
      error_message: null,
    };

    return new Promise((resolve, reject) => {
      const tx = db.transaction([OUTBOX_STORE], 'readwrite');
      const store = tx.objectStore(OUTBOX_STORE);
      const req = store.add(item);
      req.onsuccess = () => resolve(item);
      req.onerror = () => reject(req.error);
    });
  },

  /**
   * Retrieves pending or failed outbox items for the current user.
   */
  async getPendingQueue(userId: string): Promise<OutboxItem[]> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([OUTBOX_STORE], 'readonly');
      const store = tx.objectStore(OUTBOX_STORE);
      const index = store.index('by_user_id');
      const req = index.getAll(IDBKeyRange.only(userId));

      req.onsuccess = () => {
        const all: OutboxItem[] = req.result || [];
        const pending = all.filter((item) => item.status === 'pending' || item.status === 'failed');
        // Sort by creation time
        pending.sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());
        resolve(pending);
      };
      req.onerror = () => reject(req.error);
    });
  },

  /**
   * Retrieves all outbox items for the current user.
   */
  async getAllQueueItems(userId: string): Promise<OutboxItem[]> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([OUTBOX_STORE], 'readonly');
      const store = tx.objectStore(OUTBOX_STORE);
      const index = store.index('by_user_id');
      const req = index.getAll(IDBKeyRange.only(userId));
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  },

  /**
   * Updates an outbox item (e.g. status, retry_count, error).
   */
  async updateQueueItem(id: string, updates: Partial<OutboxItem>): Promise<void> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([OUTBOX_STORE], 'readwrite');
      const store = tx.objectStore(OUTBOX_STORE);
      const getReq = store.get(id);

      getReq.onsuccess = () => {
        const existing = getReq.result;
        if (!existing) {
          resolve();
          return;
        }
        const updated = { ...existing, ...updates };
        const putReq = store.put(updated);
        putReq.onsuccess = () => resolve();
        putReq.onerror = () => reject(putReq.error);
      };
      getReq.onerror = () => reject(getReq.error);
    });
  },

  /**
   * Removes an item from the outbox after successful synchronization.
   */
  async removeQueueItem(id: string): Promise<void> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([OUTBOX_STORE], 'readwrite');
      const store = tx.objectStore(OUTBOX_STORE);
      const req = store.delete(id);
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
  },

  /**
   * Caches leads locally for the given user to enable offline browsing.
   */
  async cacheUserLeads(userId: string, leads: Lead[]): Promise<void> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([CACHE_STORE], 'readwrite');
      const store = tx.objectStore(CACHE_STORE);

      const now = new Date().toISOString();
      for (const lead of leads) {
        store.put({
          id: `${userId}_${lead.id}`,
          user_id: userId,
          lead,
          cached_at: now,
        });
      }

      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  },

  /**
   * Retrieves cached leads for the current user when offline.
   */
  async getCachedUserLeads(userId: string): Promise<Lead[]> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([CACHE_STORE], 'readonly');
      const store = tx.objectStore(CACHE_STORE);
      const index = store.index('by_user_id');
      const req = index.getAll(IDBKeyRange.only(userId));

      req.onsuccess = () => {
        const records = req.result || [];
        resolve(records.map((r: any) => r.lead));
      };
      req.onerror = () => reject(req.error);
    });
  },

  /**
   * PURGE ALL USER DATA ON LOGOUT OR ACCOUNT SWITCH.
   * Guarantees that private leads or pending mutations from one user
   * can NEVER be accessed by another user logging into the same browser.
   */
  async clearUserOfflineData(userId: string): Promise<void> {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const tx = db.transaction([OUTBOX_STORE, CACHE_STORE], 'readwrite');
      const outboxStore = tx.objectStore(OUTBOX_STORE);
      const cacheStore = tx.objectStore(CACHE_STORE);

      // Purge outbox items for this user
      const outboxIndex = outboxStore.index('by_user_id');
      const outboxReq = outboxIndex.getAllKeys(IDBKeyRange.only(userId));
      outboxReq.onsuccess = () => {
        for (const key of outboxReq.result) {
          outboxStore.delete(key);
        }
      };

      // Purge cached leads for this user
      const cacheIndex = cacheStore.index('by_user_id');
      const cacheReq = cacheIndex.getAllKeys(IDBKeyRange.only(userId));
      cacheReq.onsuccess = () => {
        for (const key of cacheReq.result) {
          cacheStore.delete(key);
        }
      };

      tx.oncomplete = () => {
        console.log(`[OfflineStore] Successfully cleared private offline data for user ${userId}`);
        resolve();
      };
      tx.onerror = () => reject(tx.error);
    });
  },
};
