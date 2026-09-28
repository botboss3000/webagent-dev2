/* ═══════════════════════════════════════════════════════════════════════════
 * GENUI STATE PERSISTENCE — STANDARD SNIPPET (v1)
 * ===========================================================================
 * Paste this block into any genui page's <script> to persist its UI state to
 * localStorage so a reopen restores exactly where the user left off.
 *
 * WHY TWO LAYERS?
 *   The genui HOST (ui/main-panel/genui/js/genui.js) already persists the
 *   generic stuff automatically — page scroll (#genui-host scrollTop), inner
 *   panel scrolls, <details> open state, checkbox/radio/select values,
 *   aria-expanded, and the last page slug. Pages never need to re-implement
 *   those.
 *   This block covers the SEMANTIC state only the page knows: which record is
 *   open (project/tab/card), which sub-item is expanded, the active search
 *   query, etc. Save it whenever that state changes; restore it BEFORE the
 *   first render.
 *
 * CONVENTION
 *   Key:    'genui:<slug>:state'     (one JSON blob per page)
 *   Shape:  { field: value, ... }    null/undefined fields are dropped on save
 *   Scroll: optional 'scroll' / 'scrollLeft' fields (see restoreScroll).
 *
 * HOW TO WIRE A PAGE (checklist)
 *   1. const SLUG = '<page-slug>';   // e.g. 'home'
 *   2. Define a savePageState() that gathers the page's semantic state and
 *      calls GenUIState.save(SLUG, {...}). Call it on EVERY state change
 *      (activate/select/expand/collapse/close/search), not just on unload.
 *   3. In boot, BEFORE first render: const saved = loadPageState() — apply
 *      saved.activeId / saved.openItem / etc. into the page's STATE object.
 *   4. Render, then re-apply any "open item" expansion (the element must
 *      exist by then — do it after the render call, not before).
 *   5. Restore scroll: GenUIState.restoreScroll(saved.scroll, saved.scrollLeft)
 *      once real content is on screen (it retries, so calling once at boot is
 *      enough). The host restores scroll too — this is belt-and-braces so the
 *      page works even when mounted outside the app host.
 *   6. Optional: listen for scroll on document with capture and debounce
 *      savePageState() so the 'scroll' field stays fresh.
 *
 * Reference implementation: data/user_data/admin/genui/home/index.html
 * Full spec: docs/claude/genui-state-standard.md
 * ═══════════════════════════════════════════════════════════════════════════ */
var GenUIState=(function(){
  function key(slug){return'genui:'+slug+':state'}
  function load(slug){try{var o=JSON.parse(localStorage.getItem(key(slug))||'null');return o&&typeof o==='object'?o:{}}catch(e){return{}}}
  function save(slug,state){try{var o={};for(var k in state){var v=state[k];if(v!==null&&v!==undefined)o[k]=v}if(Object.keys(o).length)localStorage.setItem(key(slug),JSON.stringify(o));else localStorage.removeItem(key(slug))}catch(e){}}
  function clear(slug){try{localStorage.removeItem(key(slug))}catch(e){}}
  /* Scroll lives on the app's #genui-host (genui mounts in a shadow root).
     The host persists it itself; these helpers let the page persist it too,
     and fall back to window scroll when the page runs outside the host. */
  function hostEl(){try{return document.getElementById('genui-host')}catch(e){return null}}
  function scrollY(){var h=hostEl();try{return h?(h.scrollTop||0):(window.pageYOffset||document.documentElement.scrollTop||0)}catch(e){return 0}}
  function scrollX(){var h=hostEl();try{return h?(h.scrollLeft||0):(window.pageXOffset||document.documentElement.scrollLeft||0)}catch(e){return 0}}
  function restoreScroll(y,sl){if(!y&&!sl)return;function go(){try{var h=hostEl();if(h){if(sl&&Math.abs(h.scrollLeft-sl)>2)h.scrollLeft=sl;if(y&&Math.abs(h.scrollTop-y)>2)h.scrollTop=y}else{window.scrollTo(sl||0,y)}}catch(e){}}go();[80,200,400,800].forEach(function(d){setTimeout(go,d)})}
  return{key:key,load:load,save:save,clear:clear,scrollY:scrollY,scrollX:scrollX,restoreScroll:restoreScroll};
})();
