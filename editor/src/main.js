/* X-Intel 协作编辑器前端内核（esbuild 打包为 web/vendor/collab.bundle.js）。
 * 栈：Yjs 13 + y-websocket(2.x 协议) + CodeMirror 6 + y-codemirror.next。
 * 服务端：pycrdt.websocket（/ws/docs/<doc_uid>，会话 Cookie 鉴权）。
 * 契约：共享类型 = Y.Text "content"；服务器 awareness 本地状态含 saved_seq（持久化确认）。
 */
import * as Y from 'yjs';
import { WebsocketProvider } from 'y-websocket';
import { yCollab } from 'y-codemirror.next';
import { EditorView, basicSetup } from 'codemirror';
import { EditorState, Compartment } from '@codemirror/state';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { marked } from 'marked';

const COLORS = ['#0e7490', '#b45309', '#7c3aed', '#be185d', '#15803d', '#a16207'];

function pickColor(seed) {
  let h = 0;
  for (const ch of String(seed || 'x')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}

function createEditor(container, opts) {
  const ydoc = new Y.Doc();
  const wsScheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const provider = new WebsocketProvider(`${wsScheme}://${location.host}/ws/docs`, opts.docUid, ydoc);
  provider.awareness.setLocalStateField('user', {
    name: opts.username || '用户',
    color: pickColor(opts.username || 'x'),
  });

  const ytext = ydoc.getText('content');
  const undoManager = new Y.UndoManager(ytext);
  const roComp = new Compartment();
  const edComp = new Compartment();

  const state = EditorState.create({
    doc: ytext.toString(),
    extensions: [
      basicSetup,
      markdown({ base: markdownLanguage }),
      yCollab(ytext, provider.awareness, { undoManager }),
      roComp.of(opts.editable === false ? EditorState.readOnly.of(true) : []),
      edComp.of(EditorView.editable.of(opts.editable !== false)),
      EditorView.theme({
        '&': { fontSize: '14.5px', height: '100%' },
        '.cm-scroller': { fontFamily: "'Cascadia Mono','Consolas','Microsoft YaHei',monospace", lineHeight: '1.75' },
      }),
    ],
  });
  const view = new EditorView({ state, parent: container });
  function setEditable(on) {
    view.dispatch({ effects: [
      roComp.reconfigure(on ? [] : EditorState.readOnly.of(true)),
      edComp.reconfigure(EditorView.editable.of(on)),
    ] });
  }

  // ---- 真实保存状态（服务端持久化确认） ----
  // 2026-11-16 B05：按"本客户端广播的每个更新字节的 sha"与服务端 saved_shas 精确对账——
  // 其他成员的 ACK/全局 seq 推进不再代替本客户端确认；只有本客户端的全部待确认更新
  // 都出现在服务端已保存集合中，才清除 dirty。
  const pendingShas = [];           // FIFO：本客户端待确认更新 sha（hex）
  const textEncoder = new TextEncoder();
  async function sha256Hex(bytes) {
    const buf = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }
  let dirty = false;
  let lastSavedSeq = -1;
  const listeners = { status: [], saved: [] };
  function emit(k, v) { (listeners[k] || []).forEach((f) => { try { f(v); } catch (e) { console.error(e); } }); }
  function connState() {
    if (!provider.wsconnected) return 'offline';
    return dirty ? 'syncing' : 'saved';
  }
  ydoc.on('update', (update, origin, doc, tr) => {
    if (tr && tr.local) {
      pendingShas.push(sha256Hex(textEncoder.encode(update)));
      dirty = true;
      emit('status', connState());
    }
  });
  provider.on('status', () => emit('status', connState()));
  provider.on('sync', (s) => { if (s) emit('status', connState()); });
  function readServerSeq() {
    const states = provider.awareness.getStates();
    for (const [, st] of states) {
      if (st && st.role === 'server' && typeof st.saved_seq === 'number') return st.saved_seq;
    }
    return null;
  }
  let reconciling = false;
  provider.awareness.on('change', async () => {
    if (reconciling) return;   // 防并发对账重复弹出；本次未处理完的由下一次事件继续
    reconciling = true;
    try {
      const seq = readServerSeq();
      // 对账：服务端已保存 sha 集合（滚动窗口）——按序弹出本客户端已确认更新
      const states = provider.awareness.getStates();
      let savedSet = null;
      for (const [, st] of states) {
        if (st && st.role === 'server' && Array.isArray(st.saved_shas)) {
          savedSet = new Set(st.saved_shas);
          break;
        }
      }
      if (savedSet && pendingShas.length) {
        while (pendingShas.length) {
          const hex = await pendingShas[0];
          if (!savedSet.has(hex)) break;
          pendingShas.shift();
        }
        dirty = pendingShas.length > 0;
        emit('status', connState());
      }
      if (seq != null && seq !== lastSavedSeq) {
        lastSavedSeq = seq;
        emit('saved', seq);
      }
    } finally {
      reconciling = false;
    }
  });

  function members() {
    const out = [];
    provider.awareness.getStates().forEach((st, cid) => {
      if (st && st.user && cid !== provider.awareness.clientID) out.push(st.user);
    });
    return out;
  }
  provider.awareness.on('change', () => emit('status', connState()));

  function getText() { return ytext.toString(); }
  function insertAround(before, after = '') {
    const sel = view.state.selection.main;
    view.dispatch({ changes: { from: sel.from, to: sel.to, insert: before + view.state.sliceDoc(sel.from, sel.to) + after } });
    view.focus();
  }
  function destroy() {
    try { provider.destroy(); ydoc.destroy(); view.destroy(); } catch (e) { /* noop */ }
  }
  return {
    ydoc, provider, view, undoManager, ytext,
    getText, insertAround, members, destroy, setEditable,
    on: (k, f) => { (listeners[k] = listeners[k] || []).push(f); },
    connState, readServerSeq,
    awareness: provider.awareness,
  };
}

function renderPreview(mdText) {
  try { return marked.parse(mdText || '', { breaks: true, gfm: true }); }
  catch (e) { return '<p>预览失败</p>'; }
}

window.XCollab = { createEditor, renderPreview, pickColor };
export { createEditor, renderPreview };
