// Where the browser edition keeps sessions: IndexedDB (survives reloads; stays on this device). MemoryStore is used by tests
// and as the fallback when IndexedDB is not available (private windows).

export class MemoryStore {
  constructor() { this.sessions = new Map(); this.blobs = new Map(); }
  async load_all() { return [...this.sessions.values()].map((x) => structuredClone(x)); }
  async put_session(sid, record) { this.sessions.set(sid, structuredClone(record)); }
  async del_session(sid) { this.sessions.delete(sid); await this.del_blobs(sid + "/"); }
  async put_blob(key, blob) { this.blobs.set(key, blob); }
  async get_blob(key) { return this.blobs.get(key) || null; }
  async del_blob(key) { this.blobs.delete(key); }
  async del_blobs(prefix) { for (const k of [...this.blobs.keys()]) if (k.startsWith(prefix)) this.blobs.delete(k); }
  async blob_keys(prefix) { return [...this.blobs.keys()].filter((k) => k.startsWith(prefix)); }
}

export class IdbStore {
  constructor(name = "debrief-ai") { this.name = name; this.db = null; }
  async open() {
    if (this.db) return this.db;
    this.db = await new Promise((res, rej) => {
      const r = indexedDB.open(this.name, 1);
      r.onupgradeneeded = () => { r.result.createObjectStore("sessions"); r.result.createObjectStore("blobs"); };
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
    return this.db;
  }
  async _tx(store, mode, fn) {
    const db = await this.open();
    return new Promise((res, rej) => {
      const t = db.transaction(store, mode), s = t.objectStore(store);
      const out = fn(s);
      t.oncomplete = () => res(out && "result" in out ? out.result : out);
      t.onerror = () => rej(t.error); t.onabort = () => rej(t.error);
    });
  }
  async load_all() { return (await this._tx("sessions", "readonly", (s) => s.getAll())) || []; }
  async put_session(sid, record) { await this._tx("sessions", "readwrite", (s) => { s.put(record, sid); }); }
  async del_session(sid) { await this._tx("sessions", "readwrite", (s) => { s.delete(sid); }); await this.del_blobs(sid + "/"); }
  async put_blob(key, blob) { await this._tx("blobs", "readwrite", (s) => { s.put(blob, key); }); }
  async get_blob(key) { return (await this._tx("blobs", "readonly", (s) => s.get(key))) || null; }
  async del_blob(key) { await this._tx("blobs", "readwrite", (s) => { s.delete(key); }); }
  async blob_keys(prefix) { return ((await this._tx("blobs", "readonly", (s) => s.getAllKeys())) || []).filter((k) => String(k).startsWith(prefix)); }
  async del_blobs(prefix) { for (const k of await this.blob_keys(prefix)) await this.del_blob(k); }
}

export async function open_store() {
  try { if (typeof indexedDB !== "undefined") { const s = new IdbStore(); await s.open(); return s; } } catch (e) { /* private window */ }
  return new MemoryStore();
}
