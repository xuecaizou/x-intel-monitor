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
  let dirty = false;
  let lastSavedSeq = -1;
  const listeners = { status: [], saved: [] };
  function emit(k, v) { (listeners[k] || []).forEach((f) => { try { f(v); } catch (e) { console.error(e); } }); }
  function connState() {
    if (!provider.wsconnected) return 'offline';
    return dirty ? 'syncing' : 'saved';
  }
  ydoc.on('afterTransaction', (tr) => {
    if (tr.local) { dirty = true; emit('status', connState()); }
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
  provider.awareness.on('change', () => {
    const seq = readServerSeq();
    if (seq != null) {
      if (seq !== lastSavedSeq) {
        lastSavedSeq = seq;
        if (provider.wsconnected && dirty) { dirty = false; emit('status', 'saved'); }
        emit('saved', seq);
      }
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
