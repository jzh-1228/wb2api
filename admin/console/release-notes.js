/* Offline GFM rendering, including older releases wrapped in a markdown fence. */
window.WBReleaseNotes = (() => {
  function normalize(body) {
    const text = String(body || '').replace(/\r\n?/g, '\n').trim();
    const wrapped = text.match(/^(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*\n([\s\S]*?)\n\1[ \t]*$/i);
    return wrapped ? wrapped[2] : text;
  }
  function render(target, body) {
    const text = normalize(body);
    if (!window.marked || !window.DOMPurify) { target.textContent = text; return; }
    target.innerHTML = DOMPurify.sanitize(marked.parse(text, {gfm: true, breaks: true}), {
      ALLOWED_TAGS: ['h1','h2','h3','h4','h5','h6','p','br','hr','strong','em','del','ul','ol','li',
        'blockquote','pre','code','a','table','thead','tbody','tr','th','td','input'],
      ALLOWED_ATTR: ['href','title','align','type','checked','disabled'],
      ALLOW_DATA_ATTR: false
    });
    target.querySelectorAll('a').forEach(link => {
      const href = link.getAttribute('href') || '';
      if (!/^https?:\/\//i.test(href)) link.removeAttribute('href');
      else { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    });
    target.querySelectorAll('input').forEach(input => { input.type = 'checkbox'; input.disabled = true; });
  }
  return {normalize, render};
})();
